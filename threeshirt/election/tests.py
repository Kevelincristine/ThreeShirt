
from datetime import timedelta
from io import BytesIO
from importlib import import_module
import json
import os
from pathlib import Path
from decimal import Decimal
import subprocess
import sys
import threading
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.models import Session
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import CommandError
from django.db import (
    DatabaseError,
    IntegrityError,
    OperationalError,
    close_old_connections,
    connections,
    transaction,
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.http import FileResponse
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from django.urls import reverse
from PIL import Image

from accounts.admin import three_shirt_admin
from accounts.models import AccountAdminAudit, UserProfile

from .admin import ElectionSettingsAdmin, ShirtProposalAdmin
from .management.commands.transfer_sqlite_to_postgresql import (
    Command as SQLiteToPostgreSQLTransferCommand,
)
from .models import (
    ElectionSettings,
    ElectionResultSnapshot,
    ParticipationAuditEvent,
    ParticipationIdentity,
    VoteReplacementAuthorization,
    MaintenanceAudit,
    MaintenanceConfirmation,
    MaintenanceFileCleanup,
    PreferenceResponse,
    ShirtProposal,
    Vote,
)
from .services import (
    calculate_compatibility,
    create_result_snapshot,
    get_approved_proposal_compatibilities,
    get_approved_proposals,
    get_compatibility_tally,
    get_pending_proposal_descriptions,
    get_preference_response,
    get_vote_tally,
    is_survey_open,
    is_results_release_authorized,
    cast_vote,
    save_preference_response,
    submit_shirt_proposal,
)
from .storage import PrivateProposalStorage
from .views import (
    MaintenanceBackupError,
    _create_verified_database_backup,
    _process_maintenance_file_cleanup,
    proposal_image,
)

User = get_user_model()


def make_test_image(image_format="PNG", name=None, contents=None):
    if contents is None:
        output = BytesIO()
        Image.new("RGB", (2, 2), color=(20, 100, 180)).save(
            output,
            format=image_format,
        )
        contents = output.getvalue()

    extension = {
        "JPEG": "jpg",
        "PNG": "png",
        "WEBP": "webp",
        "GIF": "gif",
    }[image_format]
    filename = name or f"design.{extension}"
    return SimpleUploadedFile(
        filename,
        contents,
        content_type=f"image/{'jpeg' if image_format == 'JPEG' else image_format.lower()}",
    )


def make_proposal_data(image=None, **overrides):
    data = {
        "title": "Proposta de teste",
        "description": "Descrição detalhada da proposta de camiseta.",
        "image": image or make_test_image(),
        "base_color": "BLACK",
        "related_to_course": True,
        "animal": "",
        "additional_colors": ["BLUE"],
        "has_collar": True,
        "has_phrase": False,
        "phrase_text": "",
    }
    data.update(overrides)
    return data


class SupabaseStorageAuthorizationTests(SimpleTestCase):
    def test_request_uses_bearer_service_role_key(self):
        service_role_key = "test-service-role-key"
        with override_settings(
            SUPABASE_SERVICE_ROLE_KEY=service_role_key,
        ), patch("election.storage.urlopen") as mocked_urlopen:
            response = mocked_urlopen.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b""

            PrivateProposalStorage()._request(
                "GET",
                "https://storage.example/object",
            )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"),
            f"Bearer {service_role_key}",
        )


class SupabaseStorageOperationsTests(SimpleTestCase):
    @override_settings(
        SUPABASE_STORAGE_ENABLED=True,
        SUPABASE_URL="https://project.example.test",
        SUPABASE_SERVICE_ROLE_KEY="storage-test-key",
        SUPABASE_STORAGE_BUCKET="private",
    )
    def test_upload_read_exists_and_delete_use_private_storage_api(self):
        storage = PrivateProposalStorage()
        with patch.object(
            storage,
            "_request",
            side_effect=[
                (404, b""),
                (200, b""),
                (200, b"test image"),
                (200, b""),
                (200, b""),
            ],
        ) as request:
            name = storage.save(
                "proposal.png",
                ContentFile(b"test image", name="proposal.png"),
            )
            with storage.open(name, "rb") as image:
                self.assertEqual(image.read(), b"test image")
            self.assertTrue(storage.exists(name))
            storage.delete(name)

        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            ["GET", "POST", "GET", "GET", "DELETE"],
        )
        self.assertEqual(
            request.call_args_list[1].kwargs["extra_headers"]["x-upsert"],
            "false",
        )

    @override_settings(
        SUPABASE_STORAGE_ENABLED=True,
        SUPABASE_URL="https://project.example.test",
        SUPABASE_SERVICE_ROLE_KEY="storage-test-key",
        SUPABASE_STORAGE_BUCKET="private",
    )
    def test_listdir_separates_nested_objects_and_files(self):
        storage = PrivateProposalStorage(prefix="snapshots")
        with patch.object(
            storage,
            "_request",
            return_value=(
                200,
                json.dumps([
                    {"name": "snapshots/folder/image.png"},
                    {"name": "snapshots/result.json"},
                ]).encode(),
            ),
        ) as request:
            directories, files = storage.listdir("")

        self.assertEqual(directories, ["folder"])
        self.assertEqual(files, ["result.json"])
        self.assertEqual(request.call_args.args[0], "POST")


class PostgreSQLBackupTests(SimpleTestCase):
    def test_postgresql_backup_is_dumped_and_verified_without_cli_password(self):
        with tempfile.TemporaryDirectory() as backup_dir:
            database = {
                "NAME": "threeshirt_test",
                "HOST": "db.example.test",
                "PORT": "5432",
                "USER": "backup_user",
                "PASSWORD": "test-only-password",
                "OPTIONS": {"sslmode": "require"},
            }
            commands = []

            def run(command, **kwargs):
                commands.append((command, kwargs))
                if command[0] == "pg_dump":
                    backup_path = Path(command[command.index("--file") + 1])
                    backup_path.write_bytes(b"test PostgreSQL archive")
                return subprocess.CompletedProcess(command, 0)

            with (
                override_settings(MAINTENANCE_BACKUP_DIR=backup_dir),
                patch(
                    "election.views.connection",
                    SimpleNamespace(
                        vendor="postgresql",
                        settings_dict=database,
                    ),
                ),
                patch(
                    "election.views.shutil.which",
                    side_effect=lambda command: command,
                ),
                patch("election.views.subprocess.run", side_effect=run),
            ):
                result = _create_verified_database_backup()

            self.assertEqual(result["integrity"], "ok")
            self.assertEqual(len(result["sha256"]), 64)
            self.assertTrue(Path(result["path"]).is_file())
            dump_command, dump_options = commands[0]
            self.assertIn("--format=custom", dump_command)
            self.assertNotIn(database["PASSWORD"], dump_command)
            self.assertEqual(
                dump_options["env"]["PGPASSWORD"],
                database["PASSWORD"],
            )
            self.assertEqual(commands[1][0][1], "--list")

    def test_failed_postgresql_dump_prevents_a_verified_backup_result(self):
        with tempfile.TemporaryDirectory() as backup_dir:
            database = {
                "NAME": "threeshirt_test",
                "HOST": "db.example.test",
                "USER": "backup_user",
                "OPTIONS": {"sslmode": "require"},
            }
            with (
                override_settings(MAINTENANCE_BACKUP_DIR=backup_dir),
                patch(
                    "election.views.connection",
                    SimpleNamespace(
                        vendor="postgresql",
                        settings_dict=database,
                    ),
                ),
                patch(
                    "election.views.shutil.which",
                    side_effect=lambda command: command,
                ),
                patch(
                    "election.views.subprocess.run",
                    side_effect=subprocess.CalledProcessError(
                        1,
                        ["pg_dump"],
                    ),
                ),
                patch("election.views.logger.exception"),
            ):
                with self.assertRaises(MaintenanceBackupError):
                    _create_verified_database_backup()

            self.assertEqual(list(Path(backup_dir).iterdir()), [])


class TestEnvironmentIsolationSettingsTests(SimpleTestCase):
    def test_test_command_forces_local_storage_and_isolated_sqlite(self):
        script = (
            "import json, sys; sys.argv=['manage.py', 'test']; "
            "from backend import settings; "
            "print(json.dumps({"
            "'testing': settings.TESTING, "
            "'engine': settings.DATABASES['default']['ENGINE'], "
            "'database': str(settings.DATABASES['default']['NAME']), "
            "'storage': settings.SUPABASE_STORAGE_ENABLED, "
            "'runtime_dir': settings._TEST_RUNTIME_DIR.name, "
            "'media': str(settings.MEDIA_ROOT), "
            "'private_media': str(settings.PRIVATE_MEDIA_ROOT), "
            "'backups': str(settings.MAINTENANCE_BACKUP_DIR)"
            "}))"
        )
        environment = os.environ.copy()
        environment.update({
            "PYTHON_DOTENV_DISABLED": "true",
            "THREESHIRT_TESTING": "",
            "THREESHIRT_TEST_DATABASE": "sqlite",
            "DJANGO_ENV": "production",
            "DJANGO_DATABASE": "postgresql",
            "DJANGO_STORAGE": "supabase",
            "DJANGO_DEBUG": "False",
            "DATABASE_URL": (
                "postgresql://test:test@db.example.invalid:5432/testdb"
            ),
            "SUPABASE_URL": "https://project.example.test",
            "SUPABASE_SERVICE_ROLE_KEY": "test-only-storage-key",
            "SUPABASE_STORAGE_BUCKET": "private",
        })
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertTrue(configuration["testing"])
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.sqlite3",
        )
        self.assertNotEqual(
            Path(configuration["database"]).resolve(),
            (Path(__file__).resolve().parents[1] / "db.sqlite3").resolve(),
        )
        self.assertFalse(configuration["storage"])
        project_root = Path(__file__).resolve().parents[1]
        runtime_dir = Path(configuration["runtime_dir"])
        self.assertEqual(
            Path(configuration["database"]).resolve().parent,
            runtime_dir.resolve(),
        )
        self.assertEqual(
            Path(configuration["media"]).resolve().parent,
            runtime_dir.resolve(),
        )
        self.assertEqual(
            Path(configuration["private_media"]).resolve().parent,
            runtime_dir.resolve(),
        )
        self.assertEqual(
            Path(configuration["backups"]).resolve(),
            (runtime_dir / "backups").resolve(),
        )
        self.assertFalse(runtime_dir.exists())
        self.assertNotEqual(
            Path(configuration["media"]).resolve(),
            (project_root / "media").resolve(),
        )
        self.assertNotEqual(
            Path(configuration["private_media"]).resolve(),
            (project_root / "private_media").resolve(),
        )


class PostgreSQLDatabaseSettingsTests(SimpleTestCase):
    test_secret_key = (
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    )
    settings_script = (
        "import json; from backend import settings; "
        "print(json.dumps({"
        "'engine': settings.DATABASES['default']['ENGINE'], "
        "'testing': settings.TESTING, "
        "'database_name': str(settings.DATABASES['default']['NAME']), "
        "'test_database_name': str(settings.DATABASES['default'].get("
        "'TEST', {}).get('NAME', '')), "
        "'environment': settings.DJANGO_ENV, "
        "'debug': settings.DEBUG, "
        "'allowed_hosts': settings.ALLOWED_HOSTS, "
        "'cors_origins': settings.CORS_ALLOWED_ORIGINS, "
        "'csrf_origins': settings.CSRF_TRUSTED_ORIGINS, "
        "'session_cookie_secure': settings.SESSION_COOKIE_SECURE, "
        "'csrf_cookie_secure': settings.CSRF_COOKIE_SECURE, "
        "'ssl_redirect': settings.SECURE_SSL_REDIRECT, "
        "'options': settings.DATABASES['default'].get('OPTIONS', {}), "
        "'sslmode': settings.DATABASES['default'].get("
        "'OPTIONS', {}).get('sslmode'), "
        "'disable_server_side_cursors': settings.DATABASES['default'].get("
        "'DISABLE_SERVER_SIDE_CURSORS'), "
        "'conn_max_age': settings.DATABASES['default'].get('CONN_MAX_AGE'), "
        "'storage_backend': settings.DJANGO_STORAGE, "
        "'storage_enabled': settings.SUPABASE_STORAGE_ENABLED, "
        "'health_checks': settings.DATABASES['default'].get("
        "'CONN_HEALTH_CHECKS')"
        "}))"
    )

    def run_settings_import(
        self,
        *,
        render=False,
        database_url="",
        django_env=None,
        django_database=None,
        test_database=None,
        overrides=None,
    ):
        django_env = django_env or "development"
        is_production = django_env == "production"
        environment = os.environ.copy()
        environment.update({
            "PYTHON_DOTENV_DISABLED": "true",
            "RENDER": "true" if render else "",
            "DJANGO_ENV": django_env,
            "DJANGO_DATABASE": django_database or (
                "postgresql" if is_production else "sqlite"
            ),
            "DJANGO_STORAGE": "supabase" if is_production else "local",
            "THREESHIRT_TESTING": "1" if test_database else "",
            "THREESHIRT_TEST_DATABASE": test_database or "sqlite",
            "DJANGO_DEBUG": "False" if is_production else "True",
            "DJANGO_SECRET_KEY": self.test_secret_key,
            "DATABASE_URL": database_url,
            "DATABASE_SSLMODE": (
                "disable" if test_database == "postgresql" else "require"
            ),
            "DATABASE_CONN_MAX_AGE": "0",
            "ALLOWED_HOSTS": (
                "api.example.test" if is_production else ""
            ),
            "CSRF_TRUSTED_ORIGINS": (
                "https://frontend.example.test" if is_production else ""
            ),
            "CORS_ALLOWED_ORIGINS": (
                "https://frontend.example.test" if is_production else ""
            ),
            "SESSION_COOKIE_SECURE": (
                "True" if is_production else "False"
            ),
            "CSRF_COOKIE_SECURE": "True" if is_production else "False",
            "SESSION_COOKIE_SAMESITE": (
                "None" if is_production else "Lax"
            ),
            "CSRF_COOKIE_SAMESITE": "None" if is_production else "Lax",
            "SECURE_PROXY_SSL_HEADER": (
                "HTTP_X_FORWARDED_PROTO,https" if is_production else ""
            ),
            "SECURE_SSL_REDIRECT": (
                "True" if is_production else "False"
            ),
            "SUPABASE_URL": (
                "https://project.example.test" if is_production else ""
            ),
            "SUPABASE_SERVICE_ROLE_KEY": (
                "settings-test-only-storage-key" if is_production else ""
            ),
            "SUPABASE_STORAGE_BUCKET": "ThreeShirt" if is_production else "",
        })
        if overrides:
            environment.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", self.settings_script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_local_configuration_without_database_url_uses_sqlite(self):
        result = self.run_settings_import(render=False, database_url="")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("django.db.backends.sqlite3", result.stdout)

    def test_postgresql_test_database_requires_explicit_local_target(self):
        result = self.run_settings_import(
            test_database="postgresql",
            database_url=(
                "postgresql://test:test@127.0.0.1:55432/"
                "threeshirt_test_local"
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertTrue(configuration["testing"])
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.postgresql",
        )
        self.assertEqual(
            configuration["database_name"],
            "threeshirt_test_local",
        )
        self.assertEqual(
            configuration["test_database_name"],
            "test_threeshirt_test_local",
        )
        self.assertEqual(configuration["sslmode"], "disable")
        self.assertEqual(configuration["storage_backend"], "local")
        self.assertFalse(configuration["storage_enabled"])

    def test_postgresql_test_database_rejects_remote_or_unexpected_target(self):
        for database_url in (
            "postgresql://test:test@db.example.test:55432/"
            "threeshirt_test_local",
            "postgresql://test:test@127.0.0.1:5432/"
            "threeshirt_test_local",
            "postgresql://test:test@127.0.0.1:55432/production",
        ):
            with self.subTest(database_url=database_url):
                result = self.run_settings_import(
                    test_database="postgresql",
                    database_url=database_url,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "PostgreSQL tests require the dedicated local test "
                    "database.",
                    result.stderr,
                )

    def test_local_storage_remains_filesystem_even_with_credentials_present(self):
        result = self.run_settings_import(
            overrides={
                "SUPABASE_URL": "https://project.example.test",
                "SUPABASE_SERVICE_ROLE_KEY": "test-only-storage-key",
                "SUPABASE_STORAGE_BUCKET": "private",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["storage_backend"], "local")
        self.assertFalse(configuration["storage_enabled"])

    def test_local_supabase_storage_requires_explicit_selection(self):
        result = self.run_settings_import(
            overrides={
                "DJANGO_STORAGE": "supabase",
                "SUPABASE_URL": "https://project.example.test",
                "SUPABASE_SERVICE_ROLE_KEY": "test-only-storage-key",
                "SUPABASE_STORAGE_BUCKET": "private",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["storage_backend"], "supabase")
        self.assertTrue(configuration["storage_enabled"])

    def test_explicit_supabase_storage_requires_all_credentials(self):
        result = self.run_settings_import(
            overrides={
                "DJANGO_STORAGE": "supabase",
                "SUPABASE_URL": "",
                "SUPABASE_SERVICE_ROLE_KEY": "",
                "SUPABASE_STORAGE_BUCKET": "",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and "
            "SUPABASE_STORAGE_BUCKET must be configured",
            result.stderr,
        )

    def test_production_requires_the_expected_supabase_storage_bucket(self):
        result = self.run_settings_import(
            render=True,
            django_env="production",
            database_url="postgresql://test:test@db.example.test:5432/test",
            overrides={"SUPABASE_STORAGE_BUCKET": "private"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SUPABASE_STORAGE_BUCKET must be ThreeShirt in production.",
            result.stderr,
        )

    def test_local_configuration_ignores_database_url_and_uses_sqlite(self):
        result = self.run_settings_import(
            render=False,
            database_url="postgresql://test:test@db.example.test:5432/test",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("django.db.backends.sqlite3", result.stdout)

    def test_production_requires_database_url(self):
        result = self.run_settings_import(
            render=True,
            django_env="production",
            database_url="",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DATABASE_URL must be configured when PostgreSQL is enabled.",
            result.stderr,
        )

    def test_production_rejects_sqlite_backend(self):
        result = self.run_settings_import(
            django_env="production",
            django_database="sqlite",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Production requires DJANGO_DATABASE=postgresql.",
            result.stderr,
        )

    def test_production_rejects_sqlite_database_url(self):
        result = self.run_settings_import(
            render=True,
            django_env="production",
            database_url="sqlite:////tmp/three-shirt-test.sqlite3",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DATABASE_URL must be a PostgreSQL URL",
            result.stderr,
        )

    def test_production_postgresql_configuration_enforces_ssl_and_pool_safety(self):
        result = self.run_settings_import(
            render=True,
            django_env="production",
            database_url="postgresql://test:test@db.example.test:5432/test",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.postgresql",
        )
        self.assertEqual(configuration["options"]["sslmode"], "require")
        self.assertTrue(configuration["disable_server_side_cursors"])
        self.assertEqual(configuration["conn_max_age"], 0)
        self.assertTrue(configuration["storage_enabled"])
        self.assertTrue(configuration["health_checks"])

    def test_postgresql_is_selected_only_when_explicitly_enabled(self):
        result = self.run_settings_import(
            django_database="postgresql",
            database_url=(
                "postgresql://test-user:test-password@"
                "db.example.test:5432/testdb"
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.postgresql",
        )
        self.assertEqual(configuration["options"]["sslmode"], "require")
        self.assertTrue(configuration["disable_server_side_cursors"])
        self.assertEqual(configuration["conn_max_age"], 0)
        self.assertTrue(configuration["health_checks"])

    def test_production_selects_postgresql_without_render_flag(self):
        result = self.run_settings_import(
            django_env="production",
            database_url=(
                "postgresql://test-user:test-password@"
                "db.example.test:5432/testdb"
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["environment"], "production")
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.postgresql",
        )
        self.assertFalse(configuration["debug"])
        self.assertTrue(configuration["session_cookie_secure"])
        self.assertTrue(configuration["csrf_cookie_secure"])
        self.assertTrue(configuration["ssl_redirect"])
        self.assertTrue(configuration["storage_enabled"])

    def test_render_flag_does_not_select_production_or_storage(self):
        result = self.run_settings_import(
            render=True,
            django_env="development",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["environment"], "development")
        self.assertEqual(configuration["engine"], "django.db.backends.sqlite3")
        self.assertFalse(configuration["storage_enabled"])

    def test_production_without_database_url_fails_clearly(self):
        result = self.run_settings_import(django_env="production")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DATABASE_URL must be configured when PostgreSQL is enabled.",
            result.stderr,
        )

    def test_production_without_storage_configuration_fails_clearly(self):
        result = self.run_settings_import(
            django_env="production",
            database_url="postgresql://db.example.test:5432/testdb",
            overrides={
                "SUPABASE_URL": "",
                "SUPABASE_SERVICE_ROLE_KEY": "",
                "SUPABASE_STORAGE_BUCKET": "",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and "
            "SUPABASE_STORAGE_BUCKET must be configured when "
            "DJANGO_STORAGE=supabase.",
            result.stderr,
        )

    def test_postgresql_without_database_url_fails_clearly(self):
        result = self.run_settings_import(django_database="postgresql")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DATABASE_URL must be configured when PostgreSQL is enabled.",
            result.stderr,
        )

    def test_postgresql_connection_options_are_configurable(self):
        result = self.run_settings_import(
            django_database="postgresql",
            database_url=(
                "postgresql://test-user:test-password@"
                "db.example.test:5432/testdb"
            ),
            overrides={
                "DATABASE_SSLMODE": "verify-full",
                "DATABASE_CONN_MAX_AGE": "15",
                "DATABASE_DISABLE_SERVER_SIDE_CURSORS": "False",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["options"]["sslmode"], "verify-full")
        self.assertEqual(configuration["conn_max_age"], 15)
        self.assertFalse(configuration["disable_server_side_cursors"])

    def test_production_requires_strong_secret_key(self):
        result = self.run_settings_import(
            django_env="production",
            overrides={"DJANGO_SECRET_KEY": ""},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "A strong DJANGO_SECRET_KEY must be configured in production.",
            result.stderr,
        )

    def test_production_rejects_debug_enabled(self):
        result = self.run_settings_import(
            django_env="production",
            overrides={"DJANGO_DEBUG": "True"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "DJANGO_DEBUG must be false in production.",
            result.stderr,
        )

    def test_production_requires_explicit_allowed_hosts(self):
        result = self.run_settings_import(
            django_env="production",
            overrides={"ALLOWED_HOSTS": ""},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "ALLOWED_HOSTS must contain explicit hosts in production.",
            result.stderr,
        )

    def test_production_requires_explicit_cors_and_csrf_origins(self):
        for setting in ("CORS_ALLOWED_ORIGINS", "CSRF_TRUSTED_ORIGINS"):
            with self.subTest(setting=setting):
                result = self.run_settings_import(
                    django_env="production",
                    overrides={setting: ""},
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS "
                    "must be configured in production.",
                    result.stderr,
                )

    def test_production_rejects_non_https_origins(self):
        result = self.run_settings_import(
            django_env="production",
            overrides={
                "CORS_ALLOWED_ORIGINS": "http://frontend.example.test",
                "CSRF_TRUSTED_ORIGINS": "http://frontend.example.test",
            },
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Production CORS and CSRF origins must use HTTPS.",
            result.stderr,
        )

    def test_production_requires_secure_cookies_and_https_redirect(self):
        for setting in (
            "SESSION_COOKIE_SECURE",
            "CSRF_COOKIE_SECURE",
            "SECURE_SSL_REDIRECT",
        ):
            with self.subTest(setting=setting):
                result = self.run_settings_import(
                    django_env="production",
                    overrides={setting: "False"},
                )

                self.assertNotEqual(result.returncode, 0)

    def test_development_settings_remain_local_and_non_secure(self):
        result = self.run_settings_import()

        self.assertEqual(result.returncode, 0, result.stderr)
        configuration = json.loads(result.stdout)
        self.assertEqual(configuration["environment"], "development")
        self.assertEqual(
            configuration["engine"],
            "django.db.backends.sqlite3",
        )
        self.assertTrue(configuration["debug"])
        self.assertFalse(configuration["session_cookie_secure"])
        self.assertFalse(configuration["csrf_cookie_secure"])


class SQLiteToPostgreSQLTransferUnitTests(SimpleTestCase):
    def test_fixture_digest_ignores_json_object_key_order(self):
        first = '[{"model":"election.snapshot","fields":{"payload":{"b":2,"a":1}}}]'
        second = '[{"model":"election.snapshot","fields":{"payload":{"a":1,"b":2}}}]'

        self.assertEqual(
            SQLiteToPostgreSQLTransferCommand._fixture_digest(first),
            SQLiteToPostgreSQLTransferCommand._fixture_digest(second),
        )

    def test_source_file_paths_reject_absolute_and_traversal_paths(self):
        for name in (
            "../outside.png",
            r"..\outside.png",
            r"C:\outside.png",
            r"C:outside.png",
            "/outside.png",
        ):
            with self.subTest(name=name), self.assertRaises(CommandError):
                SQLiteToPostgreSQLTransferCommand._safe_relative_path(name)

    def test_source_asset_resolution_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "media"
            outside = Path(temporary_directory) / "outside.png"
            root.mkdir()
            outside.write_bytes(b"not an image")
            try:
                (root / "linked.png").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"Symlinks are unavailable: {error}")

            with self.assertRaises(CommandError):
                SQLiteToPostgreSQLTransferCommand()._register_source_asset(
                    {},
                    "",
                    "linked.png",
                    (root,),
                )


@override_settings(ADMIN_EMAILS=["dashboard_admin@escola.com"])
class StudentDashboardStatusApiTests(TestCase):
    dashboard_url = "/api/election/student/dashboard/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="dashboard_student",
            email="dashboard_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def get_dashboard(self):
        return self.client.get(self.dashboard_url)

    def test_anonymous_request_is_rejected_with_json_401(self):
        response = self.get_dashboard()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_student_without_preferences_or_vote_receives_default_status(self):
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "survey_open": False,
                "proposal_submission_open": True,
                "voting_open": False,
                "results_release_authorized": False,
                "has_preferences": False,
                "has_voted": False,
            },
        )

    def test_student_with_own_preferences_is_reported(self):
        PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_preferences"])
        self.assertFalse(response.json()["has_voted"])

    def test_student_who_voted_is_reported_without_vote_details(self):
        author = User.objects.create_user(username="dashboard_author")
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta aprovada",
            description="Descrição da proposta",
            image="shirt_proposals/dashboard.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        Vote.objects.create(student=self.student, proposal=proposal)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_voted"])
        self.assertNotIn("proposal", response.json())
        self.assertNotIn("vote", response.json())

    def test_active_admin_receives_general_status_without_personal_data(self):
        admin = User.objects.create_user(
            username="dashboard_admin",
            email="dashboard_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        PreferenceResponse.objects.create(
            student=admin,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        self.client.force_login(admin)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "survey_open": False,
                "proposal_submission_open": True,
                "voting_open": False,
                "results_release_authorized": False,
                "has_preferences": False,
                "has_voted": False,
            },
        )
        admin.refresh_from_db()
        self.assertTrue(admin.is_staff)
        self.assertFalse(admin.is_superuser)

    def test_inactive_student_session_is_rejected_as_anonymous(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_dashboard_does_not_expose_other_students_data(self):
        other_student = User.objects.create_user(
            username="dashboard_other",
            email="other-private@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        PreferenceResponse.objects.create(
            student=other_student,
            base_color="WHITE",
            related_to_course=False,
            wants_animal=True,
            preferred_animal="Animal privado",
            preferred_colors=["RED"],
            wants_collar=False,
            wants_phrase=True,
            intends_to_buy=False,
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(data),
            {
                "survey_open",
                "proposal_submission_open",
                "voting_open",
                "results_release_authorized",
                "has_preferences",
                "has_voted",
            },
        )
        self.assertNotIn("other-private@escola.com", response.content.decode())
        self.assertNotIn("Animal privado", response.content.decode())

    def test_survey_open_and_voting_closed_status(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["survey_open"])
        self.assertFalse(response.json()["voting_open"])

    def test_proposal_submission_status_matches_election_settings(self):
        self.client.force_login(self.student)
        settings = ElectionSettings.objects.create(
            proposal_submission_open=False,
        )

        closed_response = self.get_dashboard()

        self.assertEqual(closed_response.status_code, 200)
        self.assertFalse(closed_response.json()["proposal_submission_open"])

        settings.proposal_submission_open = True
        settings.save(update_fields=["proposal_submission_open"])
        open_response = self.get_dashboard()

        self.assertEqual(open_response.status_code, 200)
        self.assertTrue(open_response.json()["proposal_submission_open"])

    def test_voting_open_and_survey_closed_status(self):
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["survey_open"])
        self.assertTrue(response.json()["voting_open"])

    def test_results_release_flag_follows_domain_service(self):
        ElectionSettings.objects.create(results_release_authorized=True)
        self.client.force_login(self.student)

        response = self.get_dashboard()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["results_release_authorized"])
        self.assertNotIn("results", response.json())
        self.assertNotIn("winners", response.json())

    def test_database_failure_returns_generic_json_500(self):
        self.client.force_login(self.student)

        with patch(
            "election.views.get_preference_response",
            side_effect=DatabaseError("database details"),
        ), self.assertLogs("election.views", level="ERROR"):
            response = self.get_dashboard()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "Não foi possível consultar o painel."},
        )
        self.assertNotIn("database details", response.content.decode())


class StudentPreferencesApiTests(TestCase):
    preferences_url = "/api/election/student/preferences/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="preferences_api_student",
            email="preferences_api_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def create_preferences(self, student, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE", "RED"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse.objects.create(student=student, **data)

    def test_anonymous_visitor_receives_json_401(self):
        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_student_without_saved_preferences_receives_empty_json_state(self):
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(
            response.json(),
            {
                "has_preferences": False,
                "preferences": None,
            },
        )

    def test_student_receives_own_preference_fields_and_json_types(self):
        self.create_preferences(self.student)
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "has_preferences": True,
                "preferences": {
                    "base_color": "BLACK",
                    "related_to_course": True,
                    "wants_animal": False,
                    "preferred_animal": "",
                    "preferred_colors": ["BLUE", "RED"],
                    "wants_collar": True,
                    "wants_phrase": False,
                    "intends_to_buy": True,
                },
            },
        )
        preferences = response.json()["preferences"]
        self.assertIsInstance(preferences["base_color"], str)
        self.assertIsInstance(preferences["related_to_course"], bool)
        self.assertIsInstance(preferences["preferred_colors"], list)
        self.assertIsInstance(preferences["intends_to_buy"], bool)

    @override_settings(
        AUTHORIZED_LOGIN_EMAILS=["preferences-api-admin@gmail.com"]
    )
    def test_admin_profile_is_rejected_with_json_403(self):
        admin = User.objects.create_user(
            username="preferences_api_admin",
            email="preferences-api-admin@gmail.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"error": "Acesso permitido apenas a estudantes ativos."},
        )

    def test_query_parameters_cannot_select_another_students_preferences(self):
        other_student = User.objects.create_user(
            username="other_preferences_student",
            email="private-preferences@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        self.create_preferences(
            self.student,
            preferred_animal="Animal próprio",
        )
        self.create_preferences(
            other_student,
            wants_animal=True,
            preferred_animal="Animal privado",
        )
        self.client.force_login(self.student)

        response = self.client.get(
            f"{self.preferences_url}?user_id={other_student.pk}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["preferences"]["preferred_animal"],
            "Animal próprio",
        )
        self.assertNotIn("private-preferences@escola.com", response.content.decode())
        self.assertNotIn("Animal privado", response.content.decode())

    def test_response_contains_only_authorized_preference_fields(self):
        self.create_preferences(self.student)
        self.client.force_login(self.student)

        response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"has_preferences", "preferences"},
        )
        self.assertEqual(
            set(response.json()["preferences"]),
            {
                "base_color",
                "related_to_course",
                "wants_animal",
                "preferred_animal",
                "preferred_colors",
                "wants_collar",
                "wants_phrase",
                "intends_to_buy",
            },
        )
        body = response.content.decode()
        self.assertNotIn("student", body)
        self.assertNotIn("created_at", body)
        self.assertNotIn("updated_at", body)

    def test_database_failure_returns_generic_json_500(self):
        self.client.force_login(self.student)

        with patch(
            "election.views.get_preference_response",
            side_effect=DatabaseError("database details"),
        ), self.assertLogs("election.views", level="ERROR"):
            response = self.client.get(self.preferences_url)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "Não foi possível consultar suas preferências."},
        )
        self.assertNotIn("database details", response.content.decode())


@override_settings(ADMIN_EMAILS=["approved_admin@escola.com"])
class ApprovedStudentProposalsApiTests(TestCase):
    proposals_url = "/api/election/student/proposals/"

    def setUp(self):
        self.student = User.objects.create_user(
            username="approved_proposals_student",
            email="approved_proposals_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def create_proposal(self, title, status, author=None):
        if author is None:
            username = f"author_{title.lower().replace(' ', '_')}"
            author = User.objects.create_user(
                username=username,
                email=f"{username}@escola.com",
            )
            UserProfile.objects.create(
                user=author,
                role=UserProfile.Role.STUDENT,
            )
        return ShirtProposal.objects.create(
            author=author,
            title=title,
            description=f"Descrição visual de {title}.",
            image=f"shirt_proposals/{title.lower().replace(' ', '-')}.png",
            base_color=ShirtProposal.BaseColor.BLACK,
            related_to_course=True,
            animal="Raposa",
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=True,
            phrase_text="Nossa turma",
            status=status,
        )

    def test_anonymous_visitor_is_rejected_with_json_401(self):
        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json(),
            {"error": "Autenticação necessária."},
        )

    def test_active_student_receives_approved_proposals(self):
        proposal = self.create_proposal(
            "Proposta aprovada",
            ShirtProposal.Status.APPROVED,
        )
        own_proposal = self.create_proposal(
            "Proposta própria",
            ShirtProposal.Status.APPROVED,
            author=self.student,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [own_proposal.pk, proposal.pk],
        )
        self.assertEqual(
            response.json()["proposals"][0]["author_id"],
            self.student.pk,
        )

    def test_active_admin_can_read_approved_proposals(self):
        proposal = self.create_proposal(
            "Proposta aprovada para admin",
            ShirtProposal.Status.APPROVED,
        )
        admin = User.objects.create_user(
            username="approved_proposals_admin",
            email="approved_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        own_proposal = self.create_proposal(
            "Proposta própria do administrador",
            ShirtProposal.Status.APPROVED,
            author=admin,
        )
        self.client.force_login(admin)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [own_proposal.pk, proposal.pk],
        )
        admin.refresh_from_db()
        self.assertFalse(admin.is_superuser)

    @override_settings(
        AUTHORIZED_LOGIN_EMAILS=["approved-proposals-hybrid@school.com"]
    )
    def test_student_admin_receives_all_approved_proposals_including_own(self):
        hybrid = User.objects.create_user(
            username="approved_proposals_hybrid",
            email="approved-proposals-hybrid@school.com",
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        other_proposal = self.create_proposal(
            "Proposta de outro participante",
            ShirtProposal.Status.APPROVED,
        )
        own_proposal = self.create_proposal(
            "Proposta própria da estudante-admin",
            ShirtProposal.Status.APPROVED,
            author=hybrid,
        )
        self.client.force_login(hybrid)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [
                own_proposal.pk,
                other_proposal.pk,
            ],
        )
        self.assertEqual(
            response.json()["proposals"][0]["author_id"],
            hybrid.pk,
        )

    def test_pending_and_rejected_proposals_are_not_listed(self):
        self.create_proposal(
            "Aprovada",
            ShirtProposal.Status.APPROVED,
        )
        self.create_proposal(
            "Pendente",
            ShirtProposal.Status.PENDING,
        )
        self.create_proposal(
            "Rejeitada",
            ShirtProposal.Status.REJECTED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["title"] for item in response.json()["proposals"]],
            ["Aprovada"],
        )

    def test_empty_list_has_consistent_json_shape(self):
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"proposals": []})

    def test_response_fields_use_model_values_and_protected_image_route(self):
        proposal = self.create_proposal(
            "Camisa da turma",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)
        item = response.json()["proposals"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            item,
            {
                "id": proposal.pk,
                "author_id": proposal.author_id,
                "title": "Camisa da turma",
                "description": "Descrição visual de Camisa da turma.",
                "image_url": reverse("proposal-image", args=[proposal.pk]),
                "base_color": ShirtProposal.BaseColor.BLACK,
                "related_to_course": True,
                "animal": "Raposa",
                "additional_colors": ["BLUE"],
                "has_collar": True,
                "has_phrase": True,
                "phrase_text": "Nossa turma",
            },
        )
        self.assertIsInstance(item["id"], int)
        self.assertIsInstance(item["related_to_course"], bool)
        self.assertIsInstance(item["additional_colors"], list)

    def test_response_exposes_only_author_id_and_not_private_author_or_image_data(self):
        proposal = self.create_proposal(
            "Proposta privada",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)
        item = response.json()["proposals"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(item),
            {
                "id",
                "author_id",
                "title",
                "description",
                "image_url",
                "base_color",
                "related_to_course",
                "animal",
                "additional_colors",
                "has_collar",
                "has_phrase",
                "phrase_text",
            },
        )
        self.assertNotIn(
            "proposal-author-private@escola.com",
            response.content.decode(),
        )
        self.assertEqual(item["author_id"], proposal.author_id)
        self.assertNotIn(proposal.image.name, response.content.decode())

    def test_results_are_newest_first_with_stable_id_tiebreaker(self):
        older = self.create_proposal(
            "Mais antiga",
            ShirtProposal.Status.APPROVED,
        )
        newer = self.create_proposal(
            "Mais recente",
            ShirtProposal.Status.APPROVED,
        )
        self.client.force_login(self.student)

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()["proposals"]],
            [newer.pk, older.pk],
        )

    def test_service_rejects_inactive_and_non_student_users(self):
        admin = User.objects.create_user(username="service_non_student")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        inactive = User.objects.create_user(
            username="service_inactive_student",
            is_active=False,
        )
        UserProfile.objects.create(
            user=inactive,
            role=UserProfile.Role.STUDENT,
        )

        for user in (admin, inactive):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    get_approved_proposals(user)


class PreferenceResponseTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="estudante1",
            email="estudante1@escola.com",
            password="senha-de-teste",
        )

    def make_response(self, **overrides):
        data = {
            "student": self.student,
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse(**data)

    def test_valid_preferences_pass_validation(self):
        self.make_response().full_clean()

    def test_animal_is_required_when_selected(self):
        response = self.make_response(wants_animal=True)
        with self.assertRaises(ValidationError):
            response.full_clean()

    def test_animal_must_be_empty_when_not_selected(self):
        response = self.make_response(preferred_animal="Gato")
        with self.assertRaises(ValidationError):
            response.full_clean()

    def test_invalid_colors_are_rejected(self):
        response = self.make_response(
            preferred_colors=["BLUE", "INVALID_COLOR"]
        )
        with self.assertRaises(ValidationError):
            response.full_clean()


class ShirtProposalTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="autor1",
            email="autor1@escola.com",
            password="senha-de-teste",
        )

    def make_proposal(self, **overrides):
        data = {
            "author": self.author,
            "title": "Camiseta teste",
            "description": "Proposta para teste",
            "image": make_test_image(),
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
            "phrase_text": "",
        }
        data.update(overrides)
        return ShirtProposal(**data)

    def test_valid_proposal_passes_validation(self):
        self.make_proposal().full_clean()

    def test_phrase_is_required_when_selected(self):
        proposal = self.make_proposal(has_phrase=True)
        with self.assertRaises(ValidationError):
            proposal.full_clean()

    def test_phrase_must_be_empty_when_not_selected(self):
        proposal = self.make_proposal(phrase_text="Nossa turma")
        with self.assertRaises(ValidationError):
            proposal.full_clean()


class VoteTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="votante1",
            email="votante1@escola.com",
            password="senha-de-teste",
        )
        self.author = User.objects.create_user(
            username="autor2",
            email="autor2@escola.com",
            password="senha-de-teste",
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta aprovada",
            description="Proposta para teste",
            image="shirt_proposals/teste.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )

    def test_only_approved_proposals_can_receive_votes(self):
        self.proposal.status = ShirtProposal.Status.PENDING
        vote = Vote(student=self.student, proposal=self.proposal)

        with self.assertRaises(ValidationError):
            vote.full_clean()

    def test_student_cannot_vote_twice(self):
        Vote.objects.create(
            student=self.student,
            proposal=self.proposal,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(
                    student=self.student,
                    proposal=self.proposal,
                )


class VoteServiceTests(TransactionTestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_voter_service",
            email="student_voter_service@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.author = User.objects.create_user(
            username="voter_proposal_author",
            email="voter_proposal_author@escola.com",
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta de votação",
            description="Proposta aprovada para votação",
            image="shirt_proposals/vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )

    def test_student_can_vote_for_approved_proposal(self):
        vote = cast_vote(self.student, self.proposal.pk)

        self.assertIsNone(vote.student_id)
        self.assertEqual(vote.participation_identity.user_id, self.student.pk)
        self.assertEqual(vote.proposal, self.proposal)
        self.assertEqual(Vote.objects.count(), 1)

    def test_student_cannot_vote_for_own_proposal(self):
        self.proposal.author = self.student
        self.proposal.save(update_fields=["author"])

        with self.assertRaisesMessage(
            ValidationError,
            "Você não pode votar na sua própria proposta.",
        ):
            cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_student_admin_cannot_vote_for_any_of_multiple_own_proposals(self):
        hybrid = User.objects.create_user(username="hybrid_self_voter")
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        own_proposals = [
            self.proposal,
            ShirtProposal.objects.create(
                author=hybrid,
                title="Outra proposta própria",
                description="Segunda proposta própria",
                image="shirt_proposals/another-own.png",
                base_color="BLACK",
                related_to_course=True,
                additional_colors=["BLUE"],
                has_collar=True,
                has_phrase=False,
                phrase_text="",
                status=ShirtProposal.Status.APPROVED,
            ),
        ]
        self.proposal.author = hybrid
        self.proposal.save(update_fields=["author"])

        for proposal in own_proposals:
            with self.subTest(proposal=proposal.pk):
                with self.assertRaisesMessage(
                    ValidationError,
                    "Você não pode votar na sua própria proposta.",
                ):
                    cast_vote(hybrid, proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_student_cannot_vote_twice_through_service(self):
        cast_vote(self.student, self.proposal.pk)

        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 1)

    def test_closed_or_missing_voting_settings_reject_votes(self):
        ElectionSettings.objects.filter(pk=1).update(voting_open=False)
        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        ElectionSettings.objects.all().delete()
        with self.assertRaises(ValidationError):
            cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_pending_and_rejected_proposals_cannot_receive_votes(self):
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.REJECTED,
        ):
            with self.subTest(status=status):
                self.proposal.status = status
                self.proposal.save(update_fields=["status"])

                with self.assertRaises(ValidationError):
                    cast_vote(self.student, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_anonymous_inactive_missing_profile_and_admin_are_rejected(self):
        inactive_student = User.objects.create_user(
            username="inactive_voter",
            is_active=False,
        )
        missing_profile = User.objects.create_user(
            username="profileless_voter",
        )
        admin = User.objects.create_user(username="admin_voter")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)

        for user in (
            AnonymousUser(),
            inactive_student,
            missing_profile,
            admin,
        ):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    cast_vote(user, self.proposal.pk)

        self.assertEqual(Vote.objects.count(), 0)

    def test_vote_database_constraint_rejects_duplicate_student(self):
        Vote.objects.create(student=self.student, proposal=self.proposal)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(
                    student=self.student,
                    proposal=self.proposal,
                )

    def test_unrelated_database_operational_error_is_not_a_duplicate_vote(self):
        with patch(
            "election.services.Vote.objects.create",
            side_effect=OperationalError("disk I/O error"),
        ):
            with self.assertRaises(OperationalError):
                cast_vote(self.student, self.proposal.pk)


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class VoteConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.student = User.objects.create_user(
            username="student_concurrent_voter",
            email="student_concurrent_voter@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.author = User.objects.create_user(
            username="concurrent_voter_author",
            email="concurrent_voter_author@escola.com",
        )
        self.admin_user = User.objects.create_user(
            username="concurrent_vote_admin",
            email="admin@escola.com",
            is_staff=True,
            is_superuser=True,
        )
        self.proposal = ShirtProposal.objects.create(
            author=self.author,
            title="Proposta de votação concorrente",
            description="Proposta aprovada para o teste concorrente",
            image="shirt_proposals/concurrent-vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )

    def test_simultaneous_votes_use_independent_connections_and_store_one(self):
        start = threading.Barrier(3)
        outcomes = []
        connection_ids = []
        outcome_lock = threading.Lock()

        def submit_vote():
            close_old_connections()
            try:
                connections["default"].ensure_connection()
                connection_ids.append(connections["default"].connection)
                start.wait(timeout=10)
                student = User.objects.get(pk=self.student.pk)
                try:
                    cast_vote(student, self.proposal.pk)
                    outcome = "created"
                except ValidationError:
                    outcome = "duplicate"
                with outcome_lock:
                    outcomes.append(outcome)
            except Exception as exc:
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=submit_vote),
            threading.Thread(target=submit_vote),
        ]
        for worker in workers:
            worker.start()
        start.wait(timeout=10)
        for worker in workers:
            worker.join(timeout=20)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(set(connection_ids)), 2)
        self.assertCountEqual(outcomes, ["created", "duplicate"])
        self.assertEqual(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).count(),
            1,
        )

    def test_vote_in_progress_is_serialized_before_admin_closes_voting(self):
        vote_transaction_started = threading.Event()
        close_started = threading.Event()
        outcomes = []
        outcome_lock = threading.Lock()

        def submit_vote_in_transaction():
            close_old_connections()
            try:
                with transaction.atomic():
                    ElectionSettings.objects.get(pk=1)
                    vote_transaction_started.set()
                    if not close_started.wait(timeout=10):
                        raise TimeoutError("Admin close did not start.")
                    cast_vote(
                        User.objects.get(pk=self.student.pk),
                        self.proposal.pk,
                    )
                with outcome_lock:
                    outcomes.append("vote-created")
            except Exception as exc:
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        def close_voting():
            close_old_connections()
            try:
                if not vote_transaction_started.wait(timeout=10):
                    raise TimeoutError("Vote transaction did not start.")
                request = RequestFactory().post(
                    "/admin/election/electionsettings/"
                )
                request.user = User.objects.get(pk=self.admin_user.pk)
                settings = ElectionSettings.objects.get(pk=1)
                settings.voting_open = False
                close_started.set()
                ElectionSettingsAdmin(
                    ElectionSettings,
                    three_shirt_admin,
                ).save_model(
                    request,
                    settings,
                    form=None,
                    change=True,
                )
                with outcome_lock:
                    outcomes.append("voting-closed")
            except Exception as exc:
                close_started.set()
                with outcome_lock:
                    outcomes.append(exc)
            finally:
                connections["default"].close()

        workers = [
            threading.Thread(target=submit_vote_in_transaction),
            threading.Thread(target=close_voting),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertCountEqual(outcomes, ["vote-created", "voting-closed"])
        self.assertTrue(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )
        settings = ElectionSettings.objects.get(pk=1)
        self.assertFalse(settings.voting_open)
        self.assertIsNotNone(settings.voting_ended_at)

        proposal = ShirtProposal.objects.get(pk=self.proposal.pk)
        original_title = proposal.title
        proposal.title = "Alteração após corrida de voto e fechamento"
        request = RequestFactory().post("/admin/election/shirtproposal/")
        request.user = User.objects.get(pk=self.admin_user.pk)
        with self.assertRaises(ValidationError):
            ShirtProposalAdmin(
                ShirtProposal,
                three_shirt_admin,
            ).save_model(
                request,
                proposal,
                form=None,
                change=True,
            )
        proposal.refresh_from_db()
        self.assertEqual(proposal.title, original_title)


class ElectionSettingsTests(TestCase):
    def test_survey_and_voting_cannot_be_open_together(self):
        settings = ElectionSettings(
            survey_open=True,
            voting_open=True,
        )

        with self.assertRaises(ValidationError):
            settings.full_clean()

    def test_open_voting_requires_start_time(self):
        settings = ElectionSettings(
            survey_open=False,
            voting_open=True,
        )

        with self.assertRaises(ValidationError):
            settings.full_clean()


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class ElectionSettingsAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite(name="test-admin")
        self.model_admin = ElectionSettingsAdmin(
            ElectionSettings,
            three_shirt_admin,
        )
        self.authorized_admin = User.objects.create_user(
            username="admin_config",
            email="admin@escola.com",
            is_staff=True,
            is_superuser=True,
        )
        self.student = User.objects.create_user(
            username="student_config",
            email="student@escola.com",
            is_staff=True,
        )
        self.unauthorized_staff = User.objects.create_user(
            username="staff_config",
            email="staff@escola.com",
            is_staff=True,
            is_superuser=True,
        )

    def make_request(self, user):
        request = self.factory.get("/admin/election/electionsettings/")
        request.user = user
        return request

    def test_authorized_admin_can_add_singleton_and_change_survey_state(self):
        request = self.make_request(self.authorized_admin)

        self.assertTrue(self.model_admin.has_add_permission(request))
        self.assertTrue(self.model_admin.has_change_permission(request))

        settings = ElectionSettings(survey_open=True)
        self.model_admin.save_model(request, settings, form=None, change=False)

        settings.refresh_from_db()
        self.assertEqual(settings.pk, 1)
        self.assertTrue(settings.survey_open)
        self.assertFalse(self.model_admin.has_add_permission(request))

        settings.survey_open = False
        self.model_admin.save_model(request, settings, form=None, change=True)
        settings.refresh_from_db()
        self.assertFalse(settings.survey_open)

    def test_only_survey_state_is_exposed_for_editing(self):
        request = self.make_request(self.authorized_admin)

        self.assertEqual(
            self.model_admin.get_fields(request),
            (
                "survey_open",
                "proposal_submission_open",
                "voting_open",
                "results_release_authorized",
            ),
        )

    def test_authorized_admin_can_open_and_close_voting(self):
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        open_form = form_class(data={
            "survey_open": False,
            "proposal_submission_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        })
        self.assertTrue(open_form.is_valid(), open_form.errors)

        settings = open_form.save(commit=False)
        self.model_admin.save_model(
            request,
            settings,
            form=open_form,
            change=False,
        )
        settings.refresh_from_db()
        self.assertTrue(settings.voting_open)
        self.assertIsNotNone(settings.voting_started_at)
        self.assertIsNone(settings.voting_ended_at)

        close_form = form_class(
            data={
                "survey_open": False,
                "proposal_submission_open": False,
                "voting_open": False,
                "results_release_authorized": False,
            },
            instance=settings,
        )
        self.assertTrue(close_form.is_valid(), close_form.errors)
        settings = close_form.save(commit=False)
        self.model_admin.save_model(
            request,
            settings,
            form=close_form,
            change=True,
        )
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)
        self.assertIsNotNone(settings.voting_ended_at)

    def test_django_admin_rejects_voting_while_proposals_are_open(self):
        settings = ElectionSettings.objects.create(
            survey_open=False,
            proposal_submission_open=True,
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        form = form_class(
            data={
                "survey_open": False,
                "proposal_submission_open": True,
                "voting_open": True,
                "results_release_authorized": False,
            },
            instance=settings,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("__all__", form.errors)

    def test_admin_can_reopen_voting_when_no_votes_exist(self):
        settings = ElectionSettings.objects.create(
            voting_open=False,
            proposal_submission_open=False,
            voting_started_at=timezone.now() - timedelta(days=2),
            voting_ended_at=timezone.now() - timedelta(days=1),
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        form = form_class(data={
            "survey_open": False,
            "proposal_submission_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)

        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertTrue(settings.voting_open)
        self.assertIsNotNone(settings.voting_started_at)
        self.assertIsNone(settings.voting_ended_at)

    def test_reopening_is_revalidated_after_stale_form_and_preserves_timestamps(self):
        previous_start = timezone.now() - timedelta(days=2)
        previous_end = timezone.now() - timedelta(days=1)
        settings = ElectionSettings.objects.create(
            voting_open=False,
            proposal_submission_open=False,
            voting_started_at=previous_start,
            voting_ended_at=previous_end,
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)
        form = form_class(data={
            "survey_open": False,
            "proposal_submission_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)

        author = User.objects.create_user(username="reopen_vote_author")
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta votada",
            description="Proposta aprovada para voto",
            image="shirt_proposals/reopen-vote.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        Vote.objects.create(student=self.student, proposal=proposal)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                settings,
                form=form,
                change=True,
            )

        self.assertEqual(settings.voting_started_at, previous_start)
        self.assertEqual(settings.voting_ended_at, previous_end)
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)
        self.assertEqual(settings.voting_started_at, previous_start)
        self.assertEqual(settings.voting_ended_at, previous_end)

        fresh_form = form_class(data={
            "survey_open": False,
            "proposal_submission_open": False,
            "voting_open": True,
            "results_release_authorized": False,
        }, instance=settings)
        self.assertFalse(fresh_form.is_valid())
        self.assertIn("voting_open", fresh_form.errors)

    def test_admin_controls_results_release_without_changing_voting_state(self):
        settings = ElectionSettings.objects.create(
            voting_open=True,
            proposal_submission_open=False,
            voting_started_at=timezone.now(),
        )
        request = self.make_request(self.authorized_admin)
        form_class = self.model_admin.get_form(request)

        self.assertFalse(is_results_release_authorized())

        settings.results_release_authorized = True
        form = form_class(
            data={
                "survey_open": False,
                "proposal_submission_open": False,
                "voting_open": True,
                "results_release_authorized": True,
            },
            instance=settings,
        )
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertTrue(settings.results_release_authorized)
        self.assertTrue(settings.voting_open)
        self.assertTrue(is_results_release_authorized())

        settings.results_release_authorized = False
        form = form_class(
            data={
                "survey_open": False,
                "proposal_submission_open": False,
                "voting_open": True,
                "results_release_authorized": False,
            },
            instance=settings,
        )
        self.assertTrue(form.is_valid(), form.errors)
        settings = form.save(commit=False)
        self.model_admin.save_model(request, settings, form, change=True)

        settings.refresh_from_db()
        self.assertFalse(settings.results_release_authorized)
        self.assertTrue(settings.voting_open)

    def test_results_release_is_disabled_by_default(self):
        settings = ElectionSettings.objects.create(survey_open=False)

        self.assertFalse(settings.results_release_authorized)
        self.assertFalse(is_results_release_authorized())

    def test_unauthorized_admin_cannot_open_or_close_voting(self):
        settings = ElectionSettings.objects.create(survey_open=False)
        request = self.make_request(self.unauthorized_staff)
        settings.voting_open = True

        self.assertFalse(self.model_admin.has_change_permission(request))
        with self.assertRaises(PermissionDenied):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)

    def test_unauthorized_user_cannot_change_results_release(self):
        settings = ElectionSettings.objects.create(survey_open=False)
        request = self.make_request(self.unauthorized_staff)
        settings.results_release_authorized = True

        with self.assertRaises(PermissionDenied):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )

        settings.refresh_from_db()
        self.assertFalse(settings.results_release_authorized)

    def test_student_cannot_add_view_or_change_settings(self):
        request = self.make_request(self.student)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_module_permission(request))
        self.assertFalse(self.model_admin.has_view_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_staff_with_unauthorized_email_cannot_change_settings(self):
        request = self.make_request(self.unauthorized_staff)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_inactive_authorized_admin_cannot_change_settings(self):
        self.authorized_admin.is_active = False
        self.authorized_admin.save(update_fields=["is_active"])
        request = self.make_request(self.authorized_admin)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_settings_cannot_be_deleted_from_admin(self):
        request = self.make_request(self.authorized_admin)

        self.assertFalse(self.model_admin.has_delete_permission(request))

    def test_incompatible_survey_opening_is_not_saved(self):
        settings = ElectionSettings.objects.create(
            survey_open=False,
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        settings.survey_open = True
        request = self.make_request(self.authorized_admin)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                settings,
                form=None,
                change=True,
            )

        settings.refresh_from_db()
        self.assertFalse(settings.survey_open)
        self.assertTrue(settings.voting_open)


class ElectionResultsTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="results_author",
            email="results_author@escola.com",
        )
        UserProfile.objects.create(
            user=self.author,
            role=UserProfile.Role.STUDENT,
        )
        self.proposal_a = self.make_proposal("Proposta A")
        self.proposal_b = self.make_proposal("Proposta B")
        self.pending_proposal = self.make_proposal(
            "Proposta pendente",
            status=ShirtProposal.Status.PENDING,
        )

    def make_proposal(self, title, status=ShirtProposal.Status.APPROVED):
        return ShirtProposal.objects.create(
            author=self.author,
            title=title,
            description="Proposta para teste de apuração",
            image=f"shirt_proposals/{title.lower().replace(' ', '-')}.png",
            base_color="BLACK",
            related_to_course=True,
            animal="",
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=status,
        )

    def make_student(self, username):
        student = User.objects.create_user(
            username=username,
            email=f"{username}@escola.com",
        )
        UserProfile.objects.create(
            user=student,
            role=UserProfile.Role.STUDENT,
        )
        return student

    def make_response(self, student, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse.objects.create(
            student=student,
            **data,
        )

    def make_vote(self, username, proposal):
        student = self.make_student(username)
        return Vote.objects.create(student=student, proposal=proposal)

    def result_for(self, result, proposal):
        return next(
            item for item in result["proposals"]
            if item["proposal_id"] == proposal.pk
        )

    def test_vote_tally_counts_votes_and_calculates_percentages(self):
        self.make_vote("results_voter_1", self.proposal_a)
        self.make_vote("results_voter_2", self.proposal_a)
        self.make_vote("results_voter_3", self.proposal_b)

        result = get_vote_tally()
        proposal_a = self.result_for(result, self.proposal_a)
        proposal_b = self.result_for(result, self.proposal_b)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["total_votes"], 3)
        self.assertEqual(proposal_a["vote_count"], 2)
        self.assertEqual(proposal_b["vote_count"], 1)
        self.assertAlmostEqual(float(proposal_a["percentage"]), 66.6667, places=3)
        self.assertAlmostEqual(float(proposal_b["percentage"]), 33.3333, places=3)
        self.assertAlmostEqual(
            float(proposal_a["percentage"] + proposal_b["percentage"]),
            100,
            places=10,
        )
        self.assertEqual([item["proposal_id"] for item in result["winners"]], [
            self.proposal_a.pk,
        ])

    def test_no_votes_has_no_percentages_or_winner(self):
        result = get_vote_tally()

        self.assertEqual(result["status"], "no_votes")
        self.assertEqual(result["total_votes"], 0)
        self.assertEqual(result["winners"], [])
        self.assertFalse(result["is_tie"])
        self.assertTrue(
            all(item["percentage"] is None for item in result["proposals"])
        )

    def test_vote_tally_returns_all_proposals_tied_for_first(self):
        self.make_vote("tie_voter_1", self.proposal_a)
        self.make_vote("tie_voter_2", self.proposal_b)

        result = get_vote_tally()

        self.assertTrue(result["is_tie"])
        self.assertCountEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk, self.proposal_b.pk],
        )

    def test_votes_for_nonapproved_proposals_are_reported_but_not_counted(self):
        self.make_vote("valid_tally_voter", self.proposal_a)
        self.make_vote("historical_pending_voter", self.pending_proposal)
        self.pending_proposal.status = ShirtProposal.Status.REJECTED
        self.pending_proposal.save(update_fields=["status"])

        result = get_vote_tally()

        self.assertEqual(result["total_votes"], 1)
        self.assertEqual(result["excluded_votes"], 1)
        self.assertEqual(result["excluded_proposals"], [{
            "proposal_id": self.pending_proposal.pk,
            "title": self.pending_proposal.title,
            "status": ShirtProposal.Status.REJECTED,
            "is_invalidated": False,
            "vote_count": 1,
            "reason": "not_approved",
        }])
        self.assertEqual(Vote.objects.count(), 2)
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk],
        )

    def test_self_votes_are_excluded_while_voting_is_open_and_preserved(self):
        ElectionSettings.objects.create(voting_open=True)
        Vote.objects.create(
            student=self.author,
            proposal=self.proposal_a,
        )
        self.make_vote("valid_open_tally_voter", self.proposal_b)

        result = get_vote_tally()

        self.assertEqual(Vote.objects.count(), 2)
        self.assertEqual(result["total_votes"], 1)
        self.assertEqual(self.result_for(result, self.proposal_a)["vote_count"], 0)
        self.assertEqual(self.result_for(result, self.proposal_b)["vote_count"], 1)
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_b.pk],
        )

    def test_self_votes_are_excluded_until_results_are_released(self):
        ElectionSettings.objects.create(
            voting_open=False,
            results_release_authorized=False,
        )
        Vote.objects.create(
            student=self.author,
            proposal=self.proposal_a,
        )

        result = get_vote_tally()

        self.assertEqual(Vote.objects.count(), 1)
        self.assertEqual(result["total_votes"], 0)
        self.assertEqual(result["status"], "no_votes")

    def test_released_closed_results_preserve_existing_self_votes(self):
        ElectionSettings.objects.create(
            voting_open=False,
            results_release_authorized=True,
        )
        Vote.objects.create(
            student=self.author,
            proposal=self.proposal_a,
        )

        result = get_vote_tally()

        self.assertEqual(Vote.objects.count(), 1)
        self.assertEqual(result["total_votes"], 1)
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk],
        )

    def test_self_votes_do_not_change_preference_compatibility(self):
        response_student = self.make_student("self_vote_compatibility_student")
        self.make_response(response_student)
        before_vote = get_compatibility_tally()

        Vote.objects.create(student=self.author, proposal=self.proposal_a)
        after_vote = get_compatibility_tally()

        self.assertEqual(after_vote, before_vote)

    def test_compatibility_is_mean_of_valid_individual_scores(self):
        matching_student = self.make_student("matching_results_student")
        mismatching_student = self.make_student("mismatching_results_student")
        self.make_student("no_response_results_student")
        self.make_response(matching_student)
        self.make_response(
            mismatching_student,
            base_color="WHITE",
            related_to_course=False,
            preferred_colors=["RED"],
            wants_collar=False,
            wants_phrase=True,
        )

        result = get_compatibility_tally()
        proposal_result = self.result_for(result, self.proposal_a)

        self.assertEqual(proposal_result["valid_assessments"], 2)
        self.assertEqual(
            proposal_result["average_compatibility"],
            Decimal("50"),
        )
        self.assertEqual(proposal_result["status"], "available")
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk, self.proposal_b.pk],
        )

    def test_insufficient_individual_scores_are_not_counted_as_zero(self):
        student_with_data = self.make_student("partial_results_student")
        student_with_insufficient_data = self.make_student(
            "insufficient_results_student"
        )
        self.make_response(student_with_data)
        self.make_response(student_with_insufficient_data)

        with patch(
            "election.services.calculate_compatibility",
            side_effect=[(80.0, 4), (None, 2), (80.0, 4), (None, 2)],
        ):
            result = get_compatibility_tally()

        proposal_result = self.result_for(result, self.proposal_a)
        self.assertEqual(proposal_result["valid_assessments"], 1)
        self.assertEqual(
            proposal_result["average_compatibility"],
            Decimal("80"),
        )

    def test_no_valid_compatibility_data_is_not_reported_as_zero(self):
        result = get_compatibility_tally()

        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["winners"], [])
        for proposal_result in result["proposals"]:
            self.assertEqual(proposal_result["valid_assessments"], 0)
            self.assertIsNone(proposal_result["average_compatibility"])
            self.assertEqual(proposal_result["status"], "insufficient_data")

    def test_blocking_account_preserves_historical_compatibility_response(self):
        eligible_student = self.make_student("eligible_results_student")
        inactive_student = self.make_student("inactive_results_student")
        inactive_student.is_active = False
        inactive_student.save(update_fields=["is_active"])
        admin = User.objects.create_user(
            username="admin_results_profile",
            email="admin_results_profile@escola.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        profileless_user = User.objects.create_user(
            username="profileless_results_user",
            email="profileless_results_user@escola.com",
        )
        self.make_response(eligible_student)
        self.make_response(inactive_student)
        self.make_response(admin)
        self.make_response(profileless_user)

        result = get_compatibility_tally()

        for proposal in (self.proposal_a, self.proposal_b):
            proposal_result = self.result_for(result, proposal)
            self.assertEqual(proposal_result["valid_assessments"], 2)
            self.assertEqual(
                proposal_result["average_compatibility"],
                Decimal("100"),
            )

    def test_compatibility_uses_existing_individual_calculator(self):
        student = self.make_student("calculator_reuse_student")
        self.make_response(student)

        with patch(
            "election.services.calculate_compatibility",
            wraps=calculate_compatibility,
        ) as calculator:
            get_compatibility_tally()

        self.assertEqual(calculator.call_count, 2)

    def test_only_approved_proposals_are_in_compatibility_tally(self):
        result = get_compatibility_tally()

        self.assertEqual(
            {item["proposal_id"] for item in result["proposals"]},
            {self.proposal_a.pk, self.proposal_b.pk},
        )

    def test_single_most_compatible_proposal_is_selected(self):
        student = self.make_student("unique_compatibility_student")
        self.make_response(student)
        self.proposal_b.base_color = "WHITE"
        self.proposal_b.related_to_course = False
        self.proposal_b.additional_colors = ["RED"]
        self.proposal_b.has_collar = False
        self.proposal_b.has_phrase = True
        self.proposal_b.save()

        result = get_compatibility_tally()

        self.assertFalse(result["is_tie"])
        self.assertEqual(
            [item["proposal_id"] for item in result["winners"]],
            [self.proposal_a.pk],
        )

    def test_internal_results_do_not_authorize_release(self):
        self.make_vote("internal_only_voter", self.proposal_a)

        result = get_vote_tally()

        self.assertEqual(result["total_votes"], 1)
        self.assertFalse(is_results_release_authorized())


class PreferenceResponseServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_service",
            email="student_service@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        ElectionSettings.objects.create(survey_open=True)
        self.valid_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }

    def test_creates_and_reads_student_response(self):
        created = save_preference_response(self.student, self.valid_data)

        stored = PreferenceResponse.objects.get(
            participation_identity__user=self.student,
            participation_identity__is_current=True,
        )
        self.assertEqual(created.pk, stored.pk)
        self.assertEqual(stored.base_color, "BLACK")
        self.assertEqual(stored.preferred_colors, ["BLUE"])
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_edits_existing_response_while_survey_is_open(self):
        save_preference_response(self.student, self.valid_data)

        updated = save_preference_response(
            self.student,
            {"base_color": "WHITE"},
        )

        updated.refresh_from_db()
        self.assertEqual(updated.base_color, "WHITE")
        self.assertEqual(updated.preferred_colors, ["BLUE"])
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_invalid_data_is_not_persisted(self):
        invalid_data = {
            **self.valid_data,
            "preferred_colors": ["INVALID"],
        }

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, invalid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_invalid_edit_does_not_partially_change_response(self):
        existing = save_preference_response(self.student, self.valid_data)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "base_color": "WHITE",
                    "preferred_colors": ["INVALID"],
                },
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")
        self.assertEqual(existing.preferred_colors, ["BLUE"])

    def test_wrong_field_type_is_rejected_without_partial_update(self):
        existing = save_preference_response(self.student, self.valid_data)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "base_color": "WHITE",
                    "preferred_animal": None,
                },
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")
        self.assertEqual(existing.preferred_animal, "")

    def test_closed_survey_rejects_creation(self):
        ElectionSettings.objects.filter(pk=1).update(survey_open=False)

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, self.valid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_closed_survey_rejects_edit_without_changing_response(self):
        existing = save_preference_response(self.student, self.valid_data)
        ElectionSettings.objects.filter(pk=1).update(survey_open=False)

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {"base_color": "WHITE"},
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")

    def test_missing_settings_rejects_creation(self):
        ElectionSettings.objects.all().delete()

        with self.assertRaises(ValidationError):
            save_preference_response(self.student, self.valid_data)

        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_missing_settings_rejects_edit_without_changing_response(self):
        existing = save_preference_response(self.student, self.valid_data)
        ElectionSettings.objects.all().delete()

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {"base_color": "WHITE"},
            )

        existing.refresh_from_db()
        self.assertEqual(existing.base_color, "BLACK")

    def test_inactive_user_is_rejected(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])

        with self.assertRaises(PermissionDenied):
            save_preference_response(self.student, self.valid_data)

    def test_user_without_profile_is_rejected(self):
        user_without_profile = User.objects.create_user(
            username="no_profile",
            email="no_profile@escola.com",
        )

        with self.assertRaises(PermissionDenied):
            save_preference_response(user_without_profile, self.valid_data)

    def test_admin_profile_is_rejected(self):
        self.student.profile.role = UserProfile.Role.ADMIN
        self.student.profile.save(update_fields=["role"])

        with self.assertRaises(PermissionDenied):
            save_preference_response(self.student, self.valid_data)

    def test_payload_cannot_assign_another_student(self):
        other_student = User.objects.create_user(
            username="other_student",
            email="other_student@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        other_response = save_preference_response(
            other_student,
            self.valid_data,
        )

        with self.assertRaises(ValidationError):
            save_preference_response(
                self.student,
                {
                    "student": other_student.pk,
                    "base_color": "WHITE",
                },
            )

        other_response.refresh_from_db()
        self.assertEqual(other_response.base_color, "BLACK")
        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )


class PreferenceQueryServiceTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="student_queries",
            email="student_queries@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_get_preference_response_returns_own_response(self):
        response = PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )

        self.assertEqual(get_preference_response(self.student), response)

    def test_get_preference_response_returns_none_if_not_submitted(self):
        self.assertIsNone(get_preference_response(self.student))

    def test_get_preference_response_never_returns_another_students_response(self):
        other_student = User.objects.create_user(
            username="student_other_response",
            email="student_other_response@escola.com",
        )
        UserProfile.objects.create(
            user=other_student,
            role=UserProfile.Role.STUDENT,
        )
        other_response = PreferenceResponse.objects.create(
            student=other_student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )

        self.assertIsNone(get_preference_response(self.student))
        self.assertEqual(
            get_preference_response(other_student),
            other_response,
        )

    def test_protected_queries_reject_anonymous_user(self):
        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(AnonymousUser())

    def test_protected_queries_reject_user_without_profile(self):
        user_without_profile = User.objects.create_user(
            username="student_no_profile_query",
            email="student_no_profile_query@escola.com",
        )

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(user_without_profile)

    def test_protected_queries_reject_inactive_user(self):
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(self.student)

    def test_protected_queries_reject_admin_profile(self):
        self.student.profile.role = UserProfile.Role.ADMIN
        self.student.profile.save(update_fields=["role"])

        for query in (
            get_preference_response,
            get_approved_proposal_compatibilities,
        ):
            with self.subTest(query=query.__name__):
                with self.assertRaises(PermissionDenied):
                    query(self.student)

    def test_survey_state_reports_open_closed_and_missing_configuration(self):
        self.assertFalse(is_survey_open())

        ElectionSettings.objects.create(survey_open=True)
        self.assertTrue(is_survey_open())

        ElectionSettings.objects.filter(pk=1).update(survey_open=False)
        self.assertFalse(is_survey_open())

        ElectionSettings.objects.all().delete()
        self.assertFalse(is_survey_open())

    def test_compatibilities_are_empty_when_student_has_no_response(self):
        self.assertEqual(
            get_approved_proposal_compatibilities(self.student),
            [],
        )

    def test_compatibilities_include_only_approved_proposals(self):
        PreferenceResponse.objects.create(
            student=self.student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        author = User.objects.create_user(
            username="proposal_author_queries",
            email="proposal_author_queries@escola.com",
        )
        proposal_data = {
            "author": author,
            "title": "Proposta",
            "description": "Descrição",
            "image": "shirt_proposals/teste.png",
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
            "phrase_text": "",
        }
        approved = ShirtProposal.objects.create(
            **proposal_data,
            status=ShirtProposal.Status.APPROVED,
        )
        ShirtProposal.objects.create(
            **{**proposal_data, "title": "Pendente"},
            status=ShirtProposal.Status.PENDING,
        )
        ShirtProposal.objects.create(
            **{**proposal_data, "title": "Rejeitada"},
            status=ShirtProposal.Status.REJECTED,
        )

        results = get_approved_proposal_compatibilities(self.student)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], (approved, 100.0, 5))


class CompatibilityCalculationTests(TestCase):
    def make_pair(self, preference_overrides=None, proposal_overrides=None):
        preference_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
        }
        proposal_data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "animal": "",
            "additional_colors": ["BLUE"],
            "has_collar": True,
            "has_phrase": False,
        }
        preference_data.update(preference_overrides or {})
        proposal_data.update(proposal_overrides or {})
        return (
            PreferenceResponse(**preference_data),
            ShirtProposal(**proposal_data),
        )

    def test_matching_criteria_return_full_compatibility(self):
        preferences, proposal = self.make_pair()

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 5),
        )

    def test_mismatching_criteria_return_zero_compatibility(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": "WHITE",
                "related_to_course": False,
                "wants_collar": False,
                "wants_phrase": True,
            },
            proposal_overrides={
                "additional_colors": ["RED"],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (0.0, 5),
        )

    def test_colors_use_jaccard_similarity(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": ["BLUE", "RED"],
            },
            proposal_overrides={
                "additional_colors": ["BLUE", "GREEN"],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (86.67, 5),
        )

    def test_animal_names_match_without_case_or_outer_space(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": " Gato ",
            },
            proposal_overrides={
                "animal": "gAtO",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 6),
        )

    def test_different_animal_names_are_a_mismatch(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": "Gato",
            },
            proposal_overrides={
                "animal": "Cachorro",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (83.33, 6),
        )

    def test_unspecified_animal_in_proposal_is_not_comparable(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "wants_animal": True,
                "preferred_animal": "Gato",
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 5),
        )

    def test_empty_preferred_color_list_is_omitted(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 4),
        )

    def test_empty_proposal_color_list_scores_zero_when_preferred(self):
        preferences, proposal = self.make_pair(
            proposal_overrides={
                "additional_colors": [],
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (80.0, 5),
        )

    def test_missing_or_invalid_fields_are_omitted(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "wants_animal": None,
                "preferred_colors": None,
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "base_color": "INVALID",
                "related_to_course": None,
                "animal": None,
                "additional_colors": None,
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 0),
        )

    def test_no_comparable_criteria_returns_no_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "wants_animal": True,
                "preferred_animal": "",
                "preferred_colors": [],
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "base_color": None,
                "related_to_course": None,
                "animal": "",
                "additional_colors": [],
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 0),
        )

    def test_fewer_than_three_criteria_returns_no_score_and_count(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "base_color": None,
                "related_to_course": None,
                "preferred_colors": [],
                "wants_collar": True,
                "wants_phrase": False,
            },
            proposal_overrides={
                "base_color": None,
                "related_to_course": None,
                "additional_colors": [],
                "has_collar": True,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 1),
        )

    def test_exactly_two_comparable_criteria_return_no_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
                "wants_collar": None,
                "wants_phrase": None,
            },
            proposal_overrides={
                "additional_colors": [],
                "has_collar": None,
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (None, 2),
        )

    def test_three_comparable_criteria_are_enough_to_score(self):
        preferences, proposal = self.make_pair(
            preference_overrides={
                "preferred_colors": [],
                "wants_phrase": None,
            },
            proposal_overrides={
                "additional_colors": [],
                "has_phrase": None,
            },
        )

        self.assertEqual(
            calculate_compatibility(preferences, proposal),
            (100.0, 3),
        )


class ShirtProposalSubmissionTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.student = User.objects.create_user(
            username="proposal_student",
            email="proposal_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def submit(self, data=None):
        return submit_shirt_proposal(
            self.student,
            data or make_proposal_data(),
        )

    def test_valid_submission_sets_author_and_pending_status(self):
        proposal = self.submit()

        self.assertIsNone(proposal.author_id)
        self.assertEqual(proposal.author_identity.user_id, self.student.pk)
        self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)
        self.assertIsNotNone(proposal.pk)

    def test_payload_cannot_choose_author_or_status(self):
        for field, value in (
            ("author", User.objects.create_user(username="other_author")),
            ("status", ShirtProposal.Status.APPROVED),
        ):
            with self.subTest(field=field):
                data = make_proposal_data()
                data[field] = value
                with self.assertRaises(ValidationError):
                    self.submit(data)

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_blank_or_whitespace_title_and_description_are_rejected(self):
        invalid_values = (
            {"title": ""},
            {"title": "   \t"},
            {"description": ""},
            {"description": " \n "},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_title_and_description_model_limits_are_enforced(self):
        for overrides in (
            {"title": "T" * 101},
            {"description": "D" * 1001},
        ):
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_existing_visual_field_validation_is_preserved(self):
        invalid_values = (
            {"additional_colors": ["NOT_A_COLOR"]},
            {"additional_colors": ["BLUE", "BLUE"]},
            {"has_phrase": True, "phrase_text": ""},
            {"has_phrase": False, "phrase_text": "Unrequested phrase"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(**overrides))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_png_jpeg_and_webp_images_are_accepted(self):
        for image_format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(image_format=image_format):
                proposal = self.submit(
                    make_proposal_data(
                        image=make_test_image(image_format=image_format)
                    )
                )
                self.assertEqual(
                    proposal.status,
                    ShirtProposal.Status.PENDING,
                )

        self.assertEqual(ShirtProposal.objects.count(), 3)

    def test_image_content_is_checked_instead_of_filename(self):
        proposal = self.submit(
            make_proposal_data(
                image=make_test_image(name="design.gif"),
            )
        )
        self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)

        disguised_gif = make_test_image(
            image_format="GIF",
            name="design.png",
        )
        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(image=disguised_gif))

        self.assertEqual(ShirtProposal.objects.count(), 1)

    def test_non_image_and_corrupted_image_are_rejected(self):
        invalid_files = (
            SimpleUploadedFile(
                "not-image.png",
                b"plain text, not an image",
                content_type="image/png",
            ),
            make_test_image(
                image_format="PNG",
                contents=b"\x89PNG\r\n\x1a\ncorrupted",
            ),
        )
        for uploaded_file in invalid_files:
            with self.subTest(name=uploaded_file.name):
                with self.assertRaises(ValidationError):
                    self.submit(make_proposal_data(image=uploaded_file))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_image_larger_than_five_megabytes_is_rejected(self):
        valid_png = make_test_image().read()
        oversized_png = make_test_image(
            contents=valid_png + b"x" * (5 * 1024 * 1024)
        )

        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(image=oversized_png))

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_failed_validation_does_not_persist_proposal_or_file(self):
        with self.assertRaises(ValidationError):
            self.submit(make_proposal_data(title="  "))

        self.assertEqual(ShirtProposal.objects.count(), 0)
        self.assertEqual(list(Path(self.temp_dir.name).rglob("*")), [])

    def test_anonymous_inactive_missing_profile_and_admin_are_rejected(self):
        inactive_student = User.objects.create_user(
            username="inactive_proposer",
            is_active=False,
        )
        no_profile = User.objects.create_user(username="no_profile_proposer")
        admin = User.objects.create_user(username="admin_proposer")
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)

        rejected_users = (
            AnonymousUser(),
            inactive_student,
            no_profile,
            admin,
        )
        for user in rejected_users:
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    submit_shirt_proposal(user, make_proposal_data())

        self.assertEqual(ShirtProposal.objects.count(), 0)

    def test_private_storage_has_no_public_url(self):
        proposal = self.submit()
        stored_path = Path(proposal.image.storage.path(proposal.image.name))

        self.assertTrue(
            stored_path.is_relative_to(Path(self.temp_dir.name).resolve())
        )
        with self.assertRaises(ValueError):
            proposal.image.storage.url(proposal.image.name)


class PendingProposalDescriptionTests(TestCase):
    def setUp(self):
        self.student = User.objects.create_user(
            username="proposal_reader",
            email="proposal_reader@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_students_can_read_pending_description_without_image_data(self):
        proposal = ShirtProposal.objects.create(
            **make_proposal_data(image="shirt_proposals/teste.png"),
            author=self.student,
        )

        descriptions = get_pending_proposal_descriptions(self.student)

        self.assertEqual(
            descriptions,
            [{
                "id": proposal.pk,
                "title": proposal.title,
                "description": proposal.description,
            }],
        )
        self.assertNotIn("image", descriptions[0])
        self.assertNotIn("image_url", descriptions[0])

    def test_pending_description_query_rejects_unauthorized_users(self):
        for user in (
            AnonymousUser(),
            User.objects.create_user(username="proposal_reader_no_profile"),
        ):
            with self.subTest(user=user):
                with self.assertRaises(PermissionDenied):
                    get_pending_proposal_descriptions(user)


@override_settings(ADMIN_EMAILS=["proposal_admin@escola.com"])
class ShirtProposalAdminTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
            ADMIN_EMAILS=["proposal_admin@escola.com"],
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.request_factory = RequestFactory()
        self.model_admin = ShirtProposalAdmin(
            ShirtProposal,
            three_shirt_admin,
        )
        self.authorized_admin = User.objects.create_user(
            username="proposal_admin",
            email="proposal_admin@escola.com",
            is_staff=True,
        )
        self.unauthorized_admin = User.objects.create_user(
            username="proposal_unauthorized",
            email="proposal_unauthorized@escola.com",
            is_staff=True,
        )
        self.student = User.objects.create_user(
            username="proposal_admin_student",
            email="proposal_admin_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.proposal = submit_shirt_proposal(
            self.student,
            make_proposal_data(),
        )

    def request_for(self, user):
        request = self.request_factory.post("/admin/election/shirtproposal/")
        request.user = user
        return request

    def test_authorized_admin_can_approve_and_reject_proposals(self):
        request = self.request_for(self.authorized_admin)
        self.assertTrue(self.model_admin.has_change_permission(request))

        self.proposal.status = ShirtProposal.Status.APPROVED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)
        self.assertIsNotNone(self.proposal.reviewed_at)

        self.proposal.status = ShirtProposal.Status.REJECTED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.REJECTED)

    def test_proposal_without_votes_can_be_edited_after_voting_closes(self):
        ElectionSettings.objects.create(voting_open=False)
        request = self.request_for(self.authorized_admin)
        self.proposal.title = "Título revisado"
        self.proposal.status = ShirtProposal.Status.APPROVED

        self.assertTrue(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, "Título revisado")
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)

    def test_proposal_cannot_be_edited_or_moderated_while_voting_is_open(self):
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        request = self.request_for(self.authorized_admin)
        original_title = self.proposal.title
        self.proposal.title = "Título alterado durante a votação"
        self.proposal.status = ShirtProposal.Status.APPROVED

        self.assertFalse(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, original_title)
        self.assertEqual(self.proposal.status, ShirtProposal.Status.PENDING)

    def test_stale_proposal_edit_is_rejected_if_voting_opens_before_save(self):
        ElectionSettings.objects.create(voting_open=False)
        stale_proposal = ShirtProposal.objects.get(pk=self.proposal.pk)
        stale_proposal.title = "Edição de formulário antigo"
        ElectionSettings.objects.filter(pk=1).update(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        request = self.request_for(self.authorized_admin)

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                stale_proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertNotEqual(self.proposal.title, "Edição de formulário antigo")

    def test_proposal_with_votes_cannot_change_content_or_status_after_close(self):
        ElectionSettings.objects.create(voting_open=False)
        request = self.request_for(self.authorized_admin)
        self.proposal.status = ShirtProposal.Status.APPROVED
        self.model_admin.save_model(
            request,
            self.proposal,
            form=None,
            change=True,
        )
        Vote.objects.create(student=self.student, proposal=self.proposal)
        original_title = self.proposal.title

        self.proposal.title = "Título alterado depois do voto"
        self.proposal.status = ShirtProposal.Status.REJECTED
        self.assertFalse(
            self.model_admin.has_change_permission(request, self.proposal)
        )
        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.title, original_title)
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)
        self.assertEqual(Vote.objects.filter(proposal=self.proposal).count(), 1)

    def test_unauthorized_staff_and_student_cannot_moderate(self):
        for user in (self.unauthorized_admin, self.student):
            with self.subTest(user=user):
                request = self.request_for(user)
                self.assertFalse(
                    self.model_admin.has_change_permission(request)
                )

    def test_inactive_authorized_admin_cannot_moderate(self):
        self.authorized_admin.is_active = False
        self.authorized_admin.save(update_fields=["is_active"])
        request = self.request_for(self.authorized_admin)

        self.assertFalse(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_change_permission(request))

    def test_author_is_read_only_and_status_is_moderatable(self):
        request = self.request_for(self.authorized_admin)
        readonly_fields = self.model_admin.get_readonly_fields(
            request,
            self.proposal,
        )

        self.assertIn("author", readonly_fields)
        self.assertNotIn("status", readonly_fields)

    def test_admin_cannot_add_or_delete_proposals(self):
        request = self.request_for(self.authorized_admin)

        self.assertTrue(three_shirt_admin.has_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))
        self.assertFalse(self.model_admin.has_delete_permission(request))

    def test_invalid_status_is_rejected_before_persistence(self):
        request = self.request_for(self.authorized_admin)
        self.proposal.status = "INVALID"

        with self.assertRaises(ValidationError):
            self.model_admin.save_model(
                request,
                self.proposal,
                form=None,
                change=True,
            )

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.PENDING)


class ProposalImageAccessTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
            ADMIN_EMAILS=[
                "image_admin@escola.com",
                "image_admin_no_profile@escola.com",
            ],
            AUTHORIZED_LOGIN_EMAILS=["hybrid-image@escola.com"],
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.client = Client()
        self.student = User.objects.create_user(
            username="image_student",
            email="image_student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.admin = User.objects.create_user(
            username="image_admin",
            email="image_admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(
            user=self.admin,
            role=UserProfile.Role.ADMIN,
        )
        self.author = User.objects.create_user(
            username="image_author",
            email="image_author@escola.com",
        )
        UserProfile.objects.create(
            user=self.author,
            role=UserProfile.Role.STUDENT,
        )

    def create_proposal(self, status=ShirtProposal.Status.PENDING):
        proposal = submit_shirt_proposal(
            self.author,
            make_proposal_data(),
        )
        if status != ShirtProposal.Status.PENDING:
            proposal.status = status
            proposal.save(update_fields=["status"])
        return proposal

    def image_url(self, proposal):
        return reverse("proposal-image", args=[proposal.pk])

    def test_admin_can_view_pending_image_for_moderation(self):
        proposal = self.create_proposal()
        request = RequestFactory().get(self.image_url(proposal))
        request.user = self.admin
        response = proposal_image(request, proposal.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        image_file = response.file_to_stream
        self.assertEqual(image_file.read(8), b"\x89PNG\r\n\x1a\n")
        image_file.close()

    def test_student_cannot_view_pending_image(self):
        proposal = self.create_proposal()
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_student_admin_can_view_pending_image_without_django_admin_access(self):
        proposal = self.create_proposal()
        hybrid = User.objects.create_user(
            username="image_hybrid_admin",
            email="hybrid-image@escola.com",
            is_staff=False,
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        request = RequestFactory().get(self.image_url(proposal))
        request.user = hybrid
        response = proposal_image(request, proposal.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        image_file = response.file_to_stream
        self.assertEqual(image_file.read(8), b"\x89PNG\r\n\x1a\n")
        image_file.close()

    def test_unauthorized_staff_cannot_view_pending_image(self):
        proposal = self.create_proposal()
        unauthorized_staff = User.objects.create_user(
            username="image_unauthorized",
            email="image_unauthorized@escola.com",
            is_staff=True,
        )
        self.client.force_login(unauthorized_staff)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_cannot_view_pending_image(self):
        proposal = self.create_proposal()

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authorized_staff_without_admin_profile_cannot_view_pending_image(
        self,
    ):
        proposal = self.create_proposal()
        admin_without_profile = User.objects.create_user(
            username="image_admin_no_profile",
            email="image_admin_no_profile@escola.com",
            is_staff=True,
        )
        self.client.force_login(admin_without_profile)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authenticated_student_can_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        request = RequestFactory().get(self.image_url(proposal))
        request.user = self.student
        response = proposal_image(request, proposal.pk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        image_file = response.file_to_stream
        self.assertEqual(image_file.read(8), b"\x89PNG\r\n\x1a\n")
        image_file.close()

    def test_anonymous_user_cannot_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_inactive_student_cannot_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        inactive_student = User.objects.create_user(
            username="inactive_image_student",
            is_active=False,
        )
        UserProfile.objects.create(
            user=inactive_student,
            role=UserProfile.Role.STUDENT,
        )
        self.client.force_login(inactive_student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_student_profile_cannot_grant_admin_access_to_pending_image(self):
        proposal = self.create_proposal()
        staff_student = User.objects.create_user(
            username="image_staff_student",
            email="image_staff_student@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(
            user=staff_student,
            role=UserProfile.Role.STUDENT,
        )
        self.client.force_login(staff_student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_authorized_admin_can_view_approved_image(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        request = RequestFactory().get(self.image_url(proposal))
        request.user = self.admin

        response = proposal_image(request, proposal.pk)

        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.status_code, 200)
        image_file = response.file_to_stream
        self.assertFalse(image_file.closed)
        image_file.close()
        self.assertTrue(image_file.closed)

    def test_invalid_image_content_is_not_delivered(self):
        proposal = self.create_proposal(ShirtProposal.Status.APPROVED)
        image_path = Path(
            proposal.image.storage.path(proposal.image.name)
        )
        image_path.write_bytes(b"not an image")
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 404)

    def test_rejected_image_is_not_delivered_even_to_admin(self):
        proposal = self.create_proposal(ShirtProposal.Status.REJECTED)
        self.client.force_login(self.admin)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 404)

    def test_changing_proposal_id_does_not_bypass_pending_image_permission(self):
        proposal = self.create_proposal()
        self.client.force_login(self.student)

        response = self.client.get(self.image_url(proposal))

        self.assertEqual(response.status_code, 403)

    def test_image_cannot_be_fetched_from_public_media_url(self):
        proposal = self.create_proposal()

        with self.assertRaises(ValueError):
            proposal.image.url

        response = self.client.get(
            f"/media/{proposal.image.name}",
        )
        self.assertEqual(response.status_code, 404)


class PrivateImageStorageMigrationTests(TestCase):
    def test_existing_public_proposal_image_is_moved_to_private_storage(self):
        public_dir = tempfile.TemporaryDirectory()
        private_dir = tempfile.TemporaryDirectory()
        self.addCleanup(public_dir.cleanup)
        self.addCleanup(private_dir.cleanup)
        settings_override = override_settings(
            MEDIA_ROOT=Path(public_dir.name),
            PRIVATE_MEDIA_ROOT=Path(private_dir.name),
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        author = User.objects.create_user(username="legacy_image_author")
        image_name = "shirt_proposals/legacy.png"
        public_storage = FileSystemStorage(location=public_dir.name)
        public_storage.save(image_name, make_test_image())
        proposal = ShirtProposal.objects.create(
            author=author,
            title="Proposta legada",
            description="Imagem armazenada antes do armazenamento privado.",
            image=image_name,
            base_color="BLACK",
            related_to_course=True,
            has_collar=True,
            has_phrase=False,
        )

        migration = import_module(
            "election.migrations.0004_alter_shirtproposal_image"
        )
        app_registry = type(
            "MigrationApps",
            (),
            {"get_model": staticmethod(lambda app, model: ShirtProposal)},
        )()
        schema_editor = type(
            "SchemaEditor",
            (),
            {"connection": type("Connection", (), {"alias": "default"})()},
        )()

        migration.move_proposal_images_to_private_storage(
            app_registry,
            schema_editor,
        )

        migration.move_proposal_images_to_private_storage(
            app_registry,
            schema_editor,
        )

        proposal.refresh_from_db()
        private_file = Path(
            proposal.image.storage.path(proposal.image.name)
        )
        self.assertTrue(private_file.is_file())
        self.assertFalse(public_storage.exists(image_name))
        self.assertEqual(
            private_file.read_bytes()[:8],
            b"\x89PNG\r\n\x1a\n",
        )
        private_files = [
            path
            for path in Path(private_dir.name).rglob("*")
            if path.is_file()
        ]
        self.assertEqual(private_files, [private_file])

        with self.assertRaisesRegex(RuntimeError, "armazenamento público"):
            migration.restore_proposal_images_to_public_storage(
                app_registry,
                schema_editor,
            )

        self.assertFalse(public_storage.exists(image_name))
        self.assertTrue(private_file.is_file())
        self.assertEqual(
            len([
                path
                for path in Path(private_dir.name).rglob("*")
                if path.is_file()
            ]),
            1,
        )


@override_settings(ADMIN_EMAILS=["admin@escola.com"])
class AdminElectionApiPermissionTests(TestCase):
    dashboard_url = "/api/election/admin/dashboard/"
    progress_url = "/api/election/admin/progress/"
    user_role_url = "/api/election/admin/users/{}/role/"
    user_status_url = "/api/election/admin/users/{}/status/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="api_admin",
            email="admin@escola.com",
            is_staff=True,
        )
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMIN)
        self.student = User.objects.create_user(
            username="api_student",
            email="student@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )

    def test_admin_can_access_admin_dashboard_without_superuser_status(self):
        self.client.force_login(self.admin)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["counts"]["admins_total"])
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)

    def test_admin_cannot_create_accounts_through_the_users_endpoint(self):
        target_email = "new.student@gmail.com"
        self.client.force_login(self.admin)
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[target_email, self.admin.email],
        ):
            created = self.client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": target_email,
                    "full_name": "Nova estudante",
                    "role": UserProfile.Role.STUDENT,
                    "reason": "Cadastro inicial da estudante.",
                }),
                content_type="application/json",
            )
            self.assertEqual(created.status_code, 405, created.content)
            self.assertFalse(User.objects.filter(email=target_email).exists())
            listed = self.client.get("/api/election/admin/users/")
            self.assertEqual(listed.status_code, 200, listed.content)
            self.assertNotIn(
                target_email,
                [item["email"] for item in listed.json()["users"]],
            )

    def test_student_cannot_create_accounts_and_student_admin_cannot_create_admin(self):
        self.client.force_login(self.student)
        student_response = self.client.post(
            "/api/election/admin/users/",
            data=json.dumps({
                "email": "student-created@gmail.com",
                "full_name": "Conta indevida",
                "role": UserProfile.Role.STUDENT,
                "reason": "Tentativa não autorizada.",
            }),
            content_type="application/json",
        )
        self.assertEqual(student_response.status_code, 405)
        self.assertFalse(
            User.objects.filter(email="student-created@gmail.com").exists()
        )

        hybrid_email = "hybrid-creator@gmail.com"
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[
                hybrid_email,
                self.admin.email,
                "created.student@gmail.com",
                "created.hybrid@gmail.com",
            ],
        ):
            hybrid = User.objects.create_user(
                username="hybrid_creator",
                email=hybrid_email,
            )
            UserProfile.objects.create(
                user=hybrid,
                role=UserProfile.Role.STUDENT_ADMIN,
            )
            hybrid_client = Client()
            hybrid_client.force_login(hybrid)
            created_student = hybrid_client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": "created.student@gmail.com",
                    "full_name": "Estudante criada",
                    "role": UserProfile.Role.STUDENT,
                    "reason": "Cadastro por estudante-administrador.",
                }),
                content_type="application/json",
            )
            created_student_admin = hybrid_client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": "created.hybrid@gmail.com",
                    "full_name": "Administradora estudante criada",
                    "role": UserProfile.Role.STUDENT_ADMIN,
                    "reason": "Cadastro de estudante-administrador.",
                }),
                content_type="application/json",
            )
            created_admin = hybrid_client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": "forbidden.admin@gmail.com",
                    "full_name": "Administrador indevido",
                    "role": UserProfile.Role.ADMIN,
                    "reason": "Tentativa de promoção.",
                }),
                content_type="application/json",
            )

        self.assertEqual(created_student.status_code, 405, created_student.content)
        self.assertEqual(
            created_student_admin.status_code,
            405,
            created_student_admin.content,
        )
        self.assertEqual(created_admin.status_code, 405)
        self.assertFalse(
            User.objects.filter(email="forbidden.admin@gmail.com").exists()
        )
        self.assertFalse(
            User.objects.filter(
                email__in=["created.student@gmail.com", "created.hybrid@gmail.com"]
            ).exists()
        )

    def test_admin_cannot_create_an_allowlisted_admin_account_through_the_panel(self):
        admin_email = "new.admin@gmail.com"
        self.client.force_login(self.admin)
        with self.settings(
            ADMIN_EMAILS=[self.admin.email, admin_email],
            AUTHORIZED_LOGIN_EMAILS=[
                self.admin.email,
                "login-only@gmail.com",
            ],
        ):
            created = self.client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": admin_email,
                    "full_name": "Nova administradora",
                    "role": UserProfile.Role.ADMIN,
                    "reason": "Provisionamento autorizado.",
                }),
                content_type="application/json",
            )
            unlisted = self.client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": "unlisted.admin@gmail.com",
                    "full_name": "Administrador não autorizado",
                    "role": UserProfile.Role.ADMIN,
                    "reason": "Tentativa com endereço não autorizado.",
                }),
                content_type="application/json",
            )
            login_only = self.client.post(
                "/api/election/admin/users/",
                data=json.dumps({
                    "email": "login-only@gmail.com",
                    "full_name": "Administrador sem autorização administrativa",
                    "role": UserProfile.Role.ADMIN,
                    "reason": "O Gmail só está autorizado para login estudantil.",
                }),
                content_type="application/json",
            )

        self.assertEqual(created.status_code, 405, created.content)
        self.assertEqual(unlisted.status_code, 405)
        self.assertEqual(login_only.status_code, 405)
        self.assertFalse(User.objects.filter(email=admin_email).exists())
        self.assertFalse(
            User.objects.filter(
                email__in=["unlisted.admin@gmail.com", "login-only@gmail.com"]
            ).exists()
        )
    def test_admin_vote_indicators_match_open_and_released_tally_rules(self):
        settings = ElectionSettings.objects.create(voting_open=True)
        own_proposal = ShirtProposal.objects.create(
            author=self.student,
            title="Proposta da estudante",
            description="Proposta de teste",
            image="shirt_proposals/admin-count-own.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        other_proposal = ShirtProposal.objects.create(
            author=self.admin,
            title="Proposta de outro usuário",
            description="Proposta de teste",
            image="shirt_proposals/admin-count-other.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        valid_voter = User.objects.create_user(username="admin_count_valid_voter")
        UserProfile.objects.create(
            user=valid_voter,
            role=UserProfile.Role.STUDENT,
        )
        Vote.objects.create(student=self.student, proposal=own_proposal)
        Vote.objects.create(student=valid_voter, proposal=other_proposal)
        self.client.force_login(self.admin)

        for endpoint in (self.dashboard_url, self.progress_url):
            with self.subTest(endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertEqual(response.status_code, 200)
                if endpoint == self.dashboard_url:
                    self.assertEqual(response.json()["counts"]["votes_cast"], 1)
                else:
                    self.assertEqual(response.json()["total_votes"], 1)

        settings.voting_open = False
        settings.results_release_authorized = True
        settings.save(update_fields=["voting_open", "results_release_authorized"])
        self.assertEqual(Vote.objects.count(), 2)

        dashboard = self.client.get(self.dashboard_url).json()
        progress = self.client.get(self.progress_url).json()
        self.assertEqual(dashboard["counts"]["votes_cast"], 2)
        self.assertEqual(progress["total_votes"], 2)
        self.assertEqual(Vote.objects.count(), 2)

    def test_student_and_anonymous_user_cannot_access_admin_dashboard(self):
        student_response = self.client.get(self.dashboard_url)
        self.assertEqual(student_response.status_code, 403)

        self.client.force_login(self.student)
        response = self.client.get(self.dashboard_url)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.get("/api/election/admin/proposals/").status_code,
            403,
        )

    def test_promoting_a_user_does_not_grant_django_admin_access(self):
        self.client.force_login(self.admin)
        target = User.objects.create_user(
            username="promoted_admin",
            email="promoted@escola.com",
        )
        UserProfile.objects.create(user=target, role=UserProfile.Role.STUDENT)

        response = self.client.post(
            self.user_role_url.format(target.pk),
            data=json.dumps({
                "role": UserProfile.Role.ADMIN,
                "reason": "Promoção autorizada para o teste.",
            }),
            content_type="application/json",
        )

        target.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(target.profile.role, UserProfile.Role.ADMIN)
        self.assertFalse(target.is_staff)
        self.assertFalse(target.is_superuser)

    def test_user_management_round_trips_all_application_roles(self):
        self.client.force_login(self.admin)
        target = User.objects.create_user(
            username="role_round_trip",
            email="roles@escola.com",
            is_staff=False,
        )
        UserProfile.objects.create(
            user=target,
            role=UserProfile.Role.STUDENT,
        )

        for role in (
            UserProfile.Role.STUDENT_ADMIN,
            UserProfile.Role.ADMIN,
            UserProfile.Role.STUDENT,
        ):
            with self.subTest(role=role):
                response = self.client.post(
                    self.user_role_url.format(target.pk),
                    data=json.dumps({
                        "role": role,
                        "reason": "Alteração autorizada para o teste.",
                    }),
                    content_type="application/json",
                )
                target.refresh_from_db()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["user"]["role"], role)
                self.assertEqual(target.profile.role, role)
                self.assertFalse(target.is_staff)

    def test_block_and_reactivation_invalidate_sessions_even_if_a_login_saves_late(self):
        target_client = Client()
        target_client.force_login(self.student)
        old_session = dict(target_client.session.items())
        old_session_key = target_client.session.session_key
        initial_revision = self.student.profile.credential_revision
        self.client.force_login(self.admin)

        blocked = self.client.post(
            self.user_status_url.format(self.student.pk),
            data=json.dumps({
                "is_active": False,
                "reason": "Verificação administrativa.",
            }),
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 200, blocked.content)
        self.student.profile.refresh_from_db()
        self.assertEqual(
            self.student.profile.credential_revision,
            initial_revision + 1,
        )

        reactivated = self.client.post(
            self.user_status_url.format(self.student.pk),
            data=json.dumps({
                "is_active": True,
                "reason": "Reativação autorizada.",
            }),
            content_type="application/json",
        )
        self.assertEqual(reactivated.status_code, 200, reactivated.content)
        self.student.profile.refresh_from_db()
        self.assertEqual(
            self.student.profile.credential_revision,
            initial_revision + 2,
        )

        late_session = SessionStore(session_key=old_session_key)
        for key, value in old_session.items():
            late_session[key] = value
        late_session.save(must_create=True)
        stale_status = target_client.get("/api/accounts/session/")
        self.assertEqual(stale_status.json(), {"authenticated": False})

    @override_settings(AUTHORIZED_LOGIN_EMAILS=["hybrid@escola.com"])
    def test_student_admin_accesses_admin_api_without_django_admin_permissions(self):
        hybrid = User.objects.create_user(
            username="hybrid_api_admin",
            email="hybrid@escola.com",
            is_staff=False,
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        self.client.force_login(hybrid)

        dashboard = self.client.get(self.dashboard_url)
        users_response = self.client.get("/api/election/admin/users/")
        hybrid_user = next(
            item
            for item in users_response.json()["users"]
            if item["id"] == hybrid.pk
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(users_response.status_code, 200)
        self.assertEqual(hybrid_user["role"], UserProfile.Role.STUDENT_ADMIN)
        self.assertFalse(hybrid_user["is_staff"])

    @override_settings(
        ADMIN_EMAILS=["admin@escola.com", "protected@school.com"],
        AUTHORIZED_LOGIN_EMAILS=["hybrid-target@school.com"],
    )
    def test_student_admin_cannot_manage_admin_email_without_profile(self):
        target = User.objects.create_user(
            username="protected_admin_without_profile",
            email="protected@school.com",
        )
        hybrid = User.objects.create_user(
            username="hybrid_cannot_target_admin",
            email="hybrid-target@school.com",
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        self.client.force_login(hybrid)

        role_response = self.client.post(
            self.user_role_url.format(target.pk),
            data=json.dumps({
                "role": UserProfile.Role.STUDENT,
                "reason": "Teste de alvo protegido.",
            }),
            content_type="application/json",
        )
        status_response = self.client.post(
            self.user_status_url.format(target.pk),
            data=json.dumps({
                "is_active": False,
                "reason": "Teste de alvo protegido.",
            }),
            content_type="application/json",
        )
        reset_response = self.client.post(
            "/api/election/admin/maintenance/preview/",
            data=json.dumps({
                "action": "reset_user_basic",
                "target_id": target.pk,
                "reason": "Teste de alvo protegido.",
            }),
            content_type="application/json",
        )

        self.assertEqual(role_response.status_code, 403)
        self.assertEqual(status_response.status_code, 403)
        self.assertEqual(reset_response.status_code, 403)
        target.refresh_from_db()
        self.assertTrue(target.is_active)
        self.assertFalse(UserProfile.objects.filter(user=target).exists())

    def test_admin_counts_include_student_admins_as_both_roles(self):
        hybrid = User.objects.create_user(username="hybrid_count")
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        self.client.force_login(self.admin)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["counts"]["students_total"], 2)
        self.assertEqual(response.json()["counts"]["admins_total"], 2)

    def test_only_active_admin_can_keep_admin_role_and_status(self):
        self.client.force_login(self.admin)

        role_response = self.client.post(
            self.user_role_url.format(self.admin.pk),
            data=json.dumps({"role": UserProfile.Role.STUDENT}),
            content_type="application/json",
        )
        status_response = self.client.post(
            self.user_status_url.format(self.admin.pk),
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )

        self.admin.refresh_from_db()
        self.assertEqual(role_response.status_code, 400)
        self.assertEqual(status_response.status_code, 400)
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.admin.profile.role, UserProfile.Role.ADMIN)

    def test_last_student_admin_cannot_be_demoted_or_deactivated(self):
        self.admin.profile.role = UserProfile.Role.STUDENT
        self.admin.profile.save(update_fields=["role"])
        hybrid = User.objects.create_user(username="last_hybrid_admin")
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        self.client.force_login(self.student)

        with patch("election.views._require_admin_user"):
            role_response = self.client.post(
                self.user_role_url.format(hybrid.pk),
                data=json.dumps({"role": UserProfile.Role.STUDENT}),
                content_type="application/json",
            )
            status_response = self.client.post(
                self.user_status_url.format(hybrid.pk),
                data=json.dumps({"is_active": False}),
                content_type="application/json",
            )

        hybrid.refresh_from_db()
        self.assertEqual(role_response.status_code, 400)
        self.assertEqual(status_response.status_code, 400)
        self.assertEqual(hybrid.profile.role, UserProfile.Role.STUDENT_ADMIN)
        self.assertTrue(hybrid.is_active)


@override_settings(ADMIN_EMAILS=["api_admin@escola.com"])
class AdminStageControlsApiTests(TestCase):
    stages_url = "/api/election/admin/stages/"

    def setUp(self):
        self.admin = User.objects.create_user(
            username="stage_api_admin",
            email="api_admin@escola.com",
            is_staff=False,
        )
        UserProfile.objects.create(
            user=self.admin,
            role=UserProfile.Role.ADMIN,
        )
        self.client.force_login(self.admin)

    def update_stage(self, stage, action):
        return self.client.post(
            self.stages_url,
            data=json.dumps({"stage": stage, "action": action}),
            content_type="application/json",
        )

    def test_voting_requires_closed_proposal_submission_and_is_idempotent(self):
        settings = ElectionSettings.objects.create(
            survey_open=False,
            proposal_submission_open=True,
        )

        rejected = self.update_stage("voting", "open")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("proposal_submission", rejected.json()["fields"])
        settings.refresh_from_db()
        self.assertFalse(settings.voting_open)

        self.assertEqual(
            self.update_stage("proposal_submission", "close").status_code,
            200,
        )
        opened = self.update_stage("voting", "open")
        self.assertEqual(opened.status_code, 200, opened.json())
        settings.refresh_from_db()
        started_at = settings.voting_started_at
        updated_at = settings.updated_at

        self.assertEqual(self.update_stage("voting", "open").status_code, 200)
        settings.refresh_from_db()
        self.assertEqual(settings.voting_started_at, started_at)
        self.assertEqual(settings.updated_at, updated_at)

        self.assertEqual(self.update_stage("voting", "close").status_code, 200)
        settings.refresh_from_db()
        ended_at = settings.voting_ended_at
        updated_at = settings.updated_at
        self.assertEqual(self.update_stage("voting", "close").status_code, 200)
        settings.refresh_from_db()
        self.assertEqual(settings.voting_ended_at, ended_at)
        self.assertEqual(settings.updated_at, updated_at)

    def test_results_release_control_matches_student_results_access(self):
        ElectionSettings.objects.create(
            survey_open=False,
            proposal_submission_open=False,
            voting_open=False,
        )
        student = User.objects.create_user(username="release_api_student")
        UserProfile.objects.create(
            user=student,
            role=UserProfile.Role.STUDENT,
        )

        opened = self.update_stage("results_release", "open")
        self.assertEqual(opened.status_code, 200)
        self.assertTrue(opened.json()["results_release_authorized"])
        self.client.force_login(student)
        self.assertEqual(
            self.client.get("/api/election/student/results/").status_code,
            200,
        )

        self.client.force_login(self.admin)
        closed = self.update_stage("results_release", "close")
        self.assertEqual(closed.status_code, 200)
        self.assertFalse(closed.json()["results_release_authorized"])
        self.client.force_login(student)
        self.assertEqual(
            self.client.get("/api/election/student/results/").status_code,
            403,
        )


@override_settings(
    ADMIN_EMAILS=["api_admin@escola.com"],
    AUTHORIZED_LOGIN_EMAILS=["hybrid_review@escola.com"],
)
class AdminProposalReviewApiTests(TestCase):
    proposals_url = "/api/election/admin/proposals/"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.temp_dir.name),
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.admin = User.objects.create_user(
            username="review_api_hybrid",
            email="hybrid_review@escola.com",
            is_staff=False,
        )
        UserProfile.objects.create(
            user=self.admin,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        self.author = User.objects.create_user(username="review_api_author")
        UserProfile.objects.create(
            user=self.author,
            role=UserProfile.Role.STUDENT,
        )
        self.proposal = submit_shirt_proposal(
            self.author,
            make_proposal_data(),
        )
        self.client.force_login(self.admin)

    def review(self):
        return self.client.post(
            f"/api/election/admin/proposals/{self.proposal.pk}/review/",
            data=json.dumps({"decision": "approve"}),
            content_type="application/json",
        )

    def create_preferences(self, student, **overrides):
        data = {
            "base_color": PreferenceResponse.BaseColor.BLACK,
            "related_to_course": True,
            "wants_animal": True,
            "preferred_animal": "Lobo",
            "preferred_colors": ["BLUE", "RED"],
            "wants_collar": False,
            "wants_phrase": True,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return PreferenceResponse.objects.create(student=student, **data)

    def test_admin_proposal_list_returns_preferences_for_exact_author(self):
        another_author = User.objects.create_user(username="second_review_author")
        UserProfile.objects.create(
            user=another_author,
            role=UserProfile.Role.STUDENT,
        )
        another_proposal = submit_shirt_proposal(
            another_author,
            make_proposal_data(),
        )
        first_preferences = self.create_preferences(self.author)
        second_preferences = self.create_preferences(
            another_author,
            base_color=PreferenceResponse.BaseColor.WHITE,
            related_to_course=False,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["GREEN"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=False,
        )

        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        items = {
            item["id"]: item for item in response.json()["proposals"]
        }
        self.assertEqual(
            items[self.proposal.pk]["preferences"],
            {
                "base_color": first_preferences.base_color,
                "related_to_course": True,
                "wants_animal": True,
                "preferred_animal": "Lobo",
                "preferred_colors": ["BLUE", "RED"],
                "wants_collar": False,
                "wants_phrase": True,
                "intends_to_buy": True,
            },
        )
        self.assertEqual(
            items[another_proposal.pk]["preferences"],
            {
                "base_color": second_preferences.base_color,
                "related_to_course": False,
                "wants_animal": False,
                "preferred_animal": "",
                "preferred_colors": ["GREEN"],
                "wants_collar": True,
                "wants_phrase": False,
                "intends_to_buy": False,
            },
        )

    def test_admin_proposal_list_marks_missing_preferences_and_is_admin_only(self):
        response = self.client.get(self.proposals_url)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["proposals"][0]["preferences"])

        self.client.force_login(self.author)
        student_response = self.client.get(self.proposals_url)

        self.assertEqual(student_response.status_code, 403)
        self.assertNotIn("preferences", student_response.json())

    def test_hybrid_can_review_but_review_is_blocked_during_voting(self):
        ElectionSettings.objects.create(
            survey_open=False,
            proposal_submission_open=False,
            voting_open=True,
            voting_started_at=timezone.now(),
        )

        response = self.review()

        self.assertEqual(response.status_code, 400)
        self.assertIn("voting", response.json()["fields"])
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.PENDING)

        ElectionSettings.objects.filter(pk=1).update(voting_open=False)
        response = self.review()
        self.assertEqual(response.status_code, 200)
        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, ShirtProposal.Status.APPROVED)
        self.assertEqual(self.proposal.reviewed_by, self.admin)


class StudentElectionActionApiTests(TestCase):
    preferences_url = "/api/election/student/preferences/"
    submissions_url = "/api/election/student/submissions/"
    vote_url = "/api/election/student/vote/"
    results_url = "/api/election/student/results/"

    def setUp(self):
        self.private_media = tempfile.TemporaryDirectory()
        settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.private_media.name),
            AUTHORIZED_LOGIN_EMAILS=[
                "action_api_admin@gmail.com",
                "submission_api_admin@gmail.com",
                "action_api_pure_admin@gmail.com",
                "hybrid_direct_self_voter@gmail.com",
                "hybrid_participant@escola.com",
            ],
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.addCleanup(self.private_media.cleanup)
        self.student = User.objects.create_user(
            username="student_action_api",
            email="student_action_api@escola.com",
        )
        UserProfile.objects.create(
            user=self.student,
            role=UserProfile.Role.STUDENT,
        )
        self.client = Client()

    def preference_data(self, **overrides):
        data = {
            "base_color": "BLACK",
            "related_to_course": True,
            "wants_animal": False,
            "preferred_animal": "",
            "preferred_colors": ["BLUE"],
            "wants_collar": True,
            "wants_phrase": False,
            "intends_to_buy": True,
        }
        data.update(overrides)
        return data

    def create_proposal(self, status=ShirtProposal.Status.APPROVED):
        author = User.objects.create_user(
            username=f"action_proposal_author_{ShirtProposal.objects.count()}",
        )
        UserProfile.objects.create(
            user=author,
            role=UserProfile.Role.STUDENT,
        )
        image_name = ShirtProposal._meta.get_field("image").storage.save(
            f"shirt_proposals/action-api-{ShirtProposal.objects.count()}.png",
            make_test_image(name="action-api.png"),
        )
        return ShirtProposal.objects.create(
            author=author,
            title="Proposta aprovada para voto",
            description="Descrição da proposta.",
            image=image_name,
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=status,
        )

    def submission_data(self, **overrides):
        data = make_proposal_data(**overrides)
        for field in ("related_to_course", "has_collar", "has_phrase"):
            data[field] = "true" if data[field] else "false"
        data["additional_colors"] = json.dumps(data["additional_colors"])
        return data

    def test_preferences_post_requires_authentication_and_open_survey(self):
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("survey", response.json()["fields"])
        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_preferences_post_saves_and_updates_only_own_response(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_preferences"])
        saved = PreferenceResponse.objects.get(
            participation_identity__user=self.student,
            participation_identity__is_current=True,
        )
        self.assertEqual(saved.base_color, "BLACK")

        changed = self.preference_data(base_color="WHITE")
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(changed),
            content_type="application/json",
        )
        saved.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved.base_color, "WHITE")
        self.assertEqual(PreferenceResponse.objects.count(), 1)

    def test_preferences_post_rejects_invalid_fields_and_nonstudent(self):
        ElectionSettings.objects.create(survey_open=True)
        self.client.force_login(self.student)
        invalid = self.preference_data(base_color="PURPLE")
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(invalid),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            PreferenceResponse.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

        admin = User.objects.create_user(
            username="action_api_admin",
            email="action_api_admin@gmail.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_student_mutations_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        payload = json.dumps(self.preference_data())

        rejected = client.post(
            self.preferences_url,
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 403)

        csrf_response = client.get("/api/accounts/csrf/")
        token = csrf_response.json()["csrfToken"]
        ElectionSettings.objects.create(survey_open=True)
        accepted = client.post(
            self.preferences_url,
            data=payload,
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(accepted.status_code, 200)

    def test_submission_post_sets_pending_and_list_is_scoped_to_author(self):
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            PRIVATE_MEDIA_ROOT=Path(temp_dir),
        ):
            self.client.force_login(self.student)
            payload = self.submission_data()
            response = self.client.post(self.submissions_url, payload)

            self.assertEqual(response.status_code, 201)
            proposal = ShirtProposal.objects.get(pk=response.json()["proposal"]["id"])
            self.assertIsNone(proposal.author_id)
            self.assertEqual(proposal.author_identity.user_id, self.student.pk)
            self.assertEqual(proposal.status, ShirtProposal.Status.PENDING)

            other = User.objects.create_user(username="other_submission_owner")
            ShirtProposal.objects.create(
                **make_proposal_data(image="shirt_proposals/other.png"),
                author=other,
            )
            response = self.client.get(self.submissions_url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [item["id"] for item in response.json()["proposals"]],
                [proposal.pk],
            )
            self.assertNotIn("image", response.json()["proposals"][0])

    def test_submission_rejects_anonymous_admin_and_invalid_image(self):
        response = self.client.get(self.submissions_url)
        self.assertEqual(response.status_code, 401)

        admin = User.objects.create_user(
            username="submission_api_admin",
            email="submission_api_admin@gmail.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(self.submissions_url)
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.student)
        invalid_payload = self.submission_data(
            image=SimpleUploadedFile(
                "invalid.png",
                b"not an image",
                content_type="image/png",
            )
        )
        response = self.client.post(self.submissions_url, invalid_payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            ShirtProposal.objects.filter(
                author_identity__user=self.student,
                author_identity__is_current=True,
            ).exists()
        )

    def test_vote_api_enforces_authentication_settings_approval_and_uniqueness(self):
        proposal = self.create_proposal()
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        rejected = self.create_proposal(ShirtProposal.Status.PENDING)
        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": rejected.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["has_voted"])
        self.assertEqual(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).count(),
            1,
        )

        duplicate = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).count(),
            1,
        )

        admin = User.objects.create_user(
            username="action_api_pure_admin",
            email="action_api_pure_admin@gmail.com",
        )
        UserProfile.objects.create(user=admin, role=UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        admin_attempt = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(admin_attempt.status_code, 403)
        self.assertEqual(Vote.objects.count(), 1)

    def test_vote_api_rejects_direct_attempt_to_vote_for_own_proposal(self):
        own_proposal = ShirtProposal.objects.create(
            author=self.student,
            title="Proposta própria",
            description="Não pode receber o voto do próprio autor.",
            image="shirt_proposals/own-api-proposal.png",
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=ShirtProposal.Status.APPROVED,
        )
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        self.client.force_login(self.student)

        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": own_proposal.pk}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Você não pode votar na sua própria proposta.",
            response.json()["fields"]["proposal"],
        )
        self.assertFalse(
            Vote.objects.filter(
                participation_identity__user=self.student,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_student_admin_vote_api_rejects_own_proposal(self):
        hybrid = User.objects.create_user(
            username="hybrid_direct_self_voter",
            email="hybrid_direct_self_voter@gmail.com",
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        own_proposal = self.create_proposal()
        own_proposal.author = hybrid
        own_proposal.save(update_fields=["author"])
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        self.client.force_login(hybrid)

        response = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": own_proposal.pk}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Você não pode votar na sua própria proposta.",
            response.json()["fields"]["proposal"],
        )
        self.assertFalse(
            Vote.objects.filter(
                participation_identity__user=hybrid,
                participation_identity__is_current=True,
            ).exists()
        )

    def test_results_api_requires_release_and_closed_voting_and_returns_aggregates(self):
        proposal = self.create_proposal()
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 401)

        self.client.force_login(self.student)
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 403)

        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
            results_release_authorized=True,
        )
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 409)

        ElectionSettings.objects.filter(pk=1).update(voting_open=False)
        Vote.objects.create(student=self.student, proposal=proposal)
        response = self.client.get(self.results_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "complete")
        self.assertEqual(data["total_votes"], 1)
        self.assertEqual(data["proposals"][0]["proposal_id"], proposal.pk)
        self.assertNotIn("student", response.content.decode())
        self.assertNotIn("email", response.content.decode())
        self.assertEqual(data["excluded_proposals"], [])

    def test_results_without_votes_reports_no_votes(self):
        ElectionSettings.objects.create(results_release_authorized=True)
        self.client.force_login(self.student)

        response = self.client.get(self.results_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_votes")
        self.assertEqual(response.json()["total_votes"], 0)
        self.assertEqual(response.json()["winners"], [])

    def test_results_include_proposal_image_and_all_survey_aggregates(self):
        proposal = self.create_proposal()
        PreferenceResponse.objects.create(
            student=self.student,
            **self.preference_data(),
        )
        second_student = User.objects.create_user(
            username="survey_results_student",
            email="survey_results_student@escola.com",
        )
        UserProfile.objects.create(
            user=second_student,
            role=UserProfile.Role.STUDENT,
        )
        PreferenceResponse.objects.create(
            student=second_student,
            **self.preference_data(
                base_color="WHITE",
                related_to_course=False,
                wants_animal=True,
                preferred_animal="Gato",
                preferred_colors=[],
                wants_collar=False,
                wants_phrase=True,
                intends_to_buy=False,
            ),
        )
        ElectionSettings.objects.create(results_release_authorized=True)
        self.client.force_login(self.student)

        response = self.client.get(self.results_url)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["proposals"][0]["image_url"],
            f"/api/election/results/snapshots/1/images/{proposal.pk}/",
        )
        survey = data["survey_results"]
        self.assertEqual(survey["respondent_count"], 2)
        self.assertEqual(len(survey["questions"]), 8)
        questions = {
            question["id"]: question for question in survey["questions"]
        }
        self.assertEqual(
            questions["base_color"]["options"],
            [
                {
                    "value": "BLACK",
                    "label": "Preta",
                    "response_count": 1,
                    "percentage": "50",
                },
                {
                    "value": "WHITE",
                    "label": "Branca",
                    "response_count": 1,
                    "percentage": "50",
                },
            ],
        )
        animal_question = questions["preferred_animal"]
        self.assertEqual(animal_question["respondent_count"], 1)
        self.assertEqual(animal_question["options"][0]["label"], "Gato")
        self.assertEqual(animal_question["options"][0]["percentage"], "100")
        color_options = {
            option["value"]: option
            for option in questions["preferred_colors"]["options"]
        }
        self.assertEqual(len(color_options), 10)
        self.assertEqual(color_options["RED"]["response_count"], 0)
        self.assertEqual(color_options["RED"]["percentage"], "0")

    def test_student_admin_can_complete_student_participation_flow(self):
        hybrid = User.objects.create_user(
            username="hybrid_participant",
            email="hybrid_participant@escola.com",
        )
        UserProfile.objects.create(
            user=hybrid,
            role=UserProfile.Role.STUDENT_ADMIN,
        )
        ElectionSettings.objects.create(
            survey_open=True,
            proposal_submission_open=True,
        )
        self.client.force_login(hybrid)

        preferences = self.client.post(
            self.preferences_url,
            data=json.dumps(self.preference_data()),
            content_type="application/json",
        )
        self.assertEqual(preferences.status_code, 200)

        submission = self.client.post(
            self.submissions_url,
            self.submission_data(),
        )
        self.assertEqual(submission.status_code, 201)
        self.assertTrue(
            ShirtProposal.objects.filter(
                author_identity__user=hybrid,
                author_identity__is_current=True,
                status=ShirtProposal.Status.PENDING,
            ).exists()
        )

        approved = self.create_proposal()
        approved_gallery = self.client.get("/api/election/student/proposals/")
        self.assertEqual(approved_gallery.status_code, 200)
        self.assertEqual(
            [item["id"] for item in approved_gallery.json()["proposals"]],
            [approved.pk],
        )
        self.assertEqual(
            approved_gallery.json()["proposals"][0]["author_id"],
            approved.author_id,
        )
        settings = ElectionSettings.objects.get(pk=1)
        settings.survey_open = False
        settings.proposal_submission_open = False
        settings.voting_open = True
        settings.voting_started_at = timezone.now()
        settings.save()
        vote = self.client.post(
            self.vote_url,
            data=json.dumps({"proposal_id": approved.pk}),
            content_type="application/json",
        )
        self.assertEqual(vote.status_code, 200)

        settings.voting_open = False
        settings.results_release_authorized = True
        settings.save()
        results = self.client.get(self.results_url)
        self.assertEqual(results.status_code, 200)
        self.assertEqual(results.json()["total_votes"], 1)

    def test_student_participation_journey_across_election_endpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir, override_settings(
            PRIVATE_MEDIA_ROOT=Path(temp_dir),
        ):
            ElectionSettings.objects.create(survey_open=True)
            self.client.force_login(self.student)

            preferences_response = self.client.post(
                self.preferences_url,
                data=json.dumps(self.preference_data()),
                content_type="application/json",
            )
            self.assertEqual(preferences_response.status_code, 200)

            submission_response = self.client.post(
                self.submissions_url,
                self.submission_data(title="Jornada completa"),
            )
            self.assertEqual(submission_response.status_code, 201)
            proposal_id = submission_response.json()["proposal"]["id"]

            own_submissions = self.client.get(self.submissions_url)
            self.assertEqual(
                own_submissions.json()["proposals"][0]["status"],
                ShirtProposal.Status.PENDING,
            )

            proposal = ShirtProposal.objects.get(pk=proposal_id)
            proposal.status = ShirtProposal.Status.APPROVED
            proposal.save(update_fields=["status"])
            proposal_from_another_student = self.create_proposal()
            gallery = self.client.get("/api/election/student/proposals/")
            self.assertEqual(
                [item["id"] for item in gallery.json()["proposals"]],
                [proposal_from_another_student.pk, proposal_id],
            )

            ElectionSettings.objects.filter(pk=1).update(
                survey_open=False,
                voting_open=True,
                voting_started_at=timezone.now(),
            )
            vote_response = self.client.post(
                self.vote_url,
                data=json.dumps({
                    "proposal_id": proposal_from_another_student.pk,
                }),
                content_type="application/json",
            )
            self.assertEqual(vote_response.status_code, 200)
            self.assertTrue(vote_response.json()["has_voted"])

            ElectionSettings.objects.filter(pk=1).update(
                voting_open=False,
                results_release_authorized=True,
                voting_ended_at=timezone.now(),
            )
            results = self.client.get(self.results_url)
            self.assertEqual(results.status_code, 200)
            self.assertEqual(results.json()["total_votes"], 1)
            self.assertEqual(
                results.json()["winners"][0]["proposal_id"],
                proposal_from_another_student.pk,
            )

            logout_response = self.client.post("/api/accounts/logout/")
            self.assertEqual(logout_response.status_code, 200)
            session_response = self.client.get("/api/accounts/session/")
            self.assertEqual(
                session_response.json(),
                {"authenticated": False},
            )


@override_settings(
    ADMIN_EMAILS=["integrity_admin@school.com"],
    AUTHORIZED_LOGIN_EMAILS=[
        "integrity_admin@school.com",
        "integrity_student_admin@school.com",
    ],
)
class AdminParticipationIntegrityTests(TransactionTestCase):
    preview_url = "/api/election/admin/maintenance/preview/"
    confirm_url = "/api/election/admin/maintenance/confirm/"

    def setUp(self):
        self.private_media = tempfile.TemporaryDirectory()
        self.maintenance_backups = tempfile.TemporaryDirectory()
        media_settings = override_settings(
            PRIVATE_MEDIA_ROOT=Path(self.private_media.name),
            MAINTENANCE_BACKUP_DIR=Path(self.maintenance_backups.name),
        )
        media_settings.enable()
        self.addCleanup(media_settings.disable)
        self.addCleanup(self.private_media.cleanup)
        self.addCleanup(self.maintenance_backups.cleanup)
        self.admin = self.create_user(
            "integrity_admin",
            UserProfile.Role.ADMIN,
        )
        self.client.force_login(self.admin)

    def create_user(self, username, role=UserProfile.Role.STUDENT):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@school.com",
            password="OriginalPassword!42",
        )
        UserProfile.objects.create(user=user, role=role)
        return user

    def create_proposal(
        self,
        author,
        status=ShirtProposal.Status.APPROVED,
        image="",
    ):
        return ShirtProposal.objects.create(
            author=author,
            title=f"Proposta de {author.username}",
            description="Descrição de teste",
            image=image,
            base_color="BLACK",
            related_to_course=True,
            additional_colors=["BLUE"],
            has_collar=True,
            has_phrase=False,
            phrase_text="",
            status=status,
        )

    def post_json(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def reset_user(self, user, action="reset_user_basic", reason="Solicitação validada"):
        preview = self.post_json(
            self.preview_url,
            {
                "action": action,
                "target_id": user.pk,
                "reason": reason,
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        result = self.post_json(
            self.confirm_url,
            {"challenge_token": preview.json()["challenge_token"]},
        )
        self.assertEqual(result.status_code, 200, result.content)
        return result.json()

    def test_basic_account_reset_preserves_historical_participation(self):
        target = self.create_user("reset_target", UserProfile.Role.STUDENT_ADMIN)
        voter = self.create_user("reset_voter")
        target_proposal = self.create_proposal(
            target,
            ShirtProposal.Status.PENDING,
        )
        voted_proposal = self.create_proposal(voter)
        response = PreferenceResponse.objects.create(
            student=target,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        vote = Vote.objects.create(student=target, proposal=voted_proposal)
        old_identity = response.participation_identity
        previous_email = target.email
        previous_username = target.username
        target_client = Client()
        target_client.force_login(target)
        previous_session_key = target_client.session.session_key
        self.assertTrue(Session.objects.filter(
            session_key=previous_session_key,
        ).exists())

        self.reset_user(target)

        target.refresh_from_db()
        target.profile.refresh_from_db()
        old_identity.refresh_from_db()
        response.refresh_from_db()
        vote.refresh_from_db()
        target_proposal.refresh_from_db()
        self.assertEqual(target.email, previous_email)
        self.assertEqual(target.username, previous_username)
        self.assertFalse(target.has_usable_password())
        self.assertEqual(target.profile.role, UserProfile.Role.STUDENT)
        self.assertEqual(target.profile.credential_revision, 1)
        self.assertIsNone(old_identity.user_id)
        self.assertFalse(old_identity.is_current)
        self.assertTrue(PreferenceResponse.objects.filter(pk=response.pk).exists())
        self.assertTrue(Vote.objects.filter(pk=vote.pk).exists())
        self.assertTrue(ShirtProposal.objects.filter(pk=target_proposal.pk).exists())
        self.assertIsNone(response.student_id)
        self.assertIsNone(vote.student_id)
        self.assertIsNone(target_proposal.author_id)
        self.assertFalse(Session.objects.filter(
            session_key=previous_session_key,
        ).exists())
        audit = AccountAdminAudit.objects.get(
            target_identifier=target.pk,
            action=AccountAdminAudit.Action.ACCOUNT_RESET,
        )
        self.assertEqual(audit.actor_id, self.admin.pk)
        self.assertEqual(audit.reason, "Solicitação validada")

    def test_full_reset_reports_and_preserves_distinct_third_party_proposals_and_images(self):
        target = self.create_user("full_reset_target")
        voter_one = self.create_user("full_reset_voter_one")
        voter_two = self.create_user("full_reset_voter_two")
        outsider = self.create_user("full_reset_outsider")
        storage = ShirtProposal._meta.get_field("image").storage
        retained_image = storage.save(
            "shirt_proposals/retained.png",
            ContentFile(b"retained-image"),
        )
        shared_image = storage.save(
            "shirt_proposals/shared.png",
            ContentFile(b"shared-image"),
        )
        retained_proposal = self.create_proposal(
            target,
            image=retained_image,
        )
        removed_proposal = self.create_proposal(
            target,
            image=shared_image,
        )
        outside_proposal = self.create_proposal(
            outsider,
            image=shared_image,
        )
        Vote.objects.create(student=voter_one, proposal=retained_proposal)
        Vote.objects.create(student=voter_two, proposal=retained_proposal)
        target_vote = Vote.objects.create(
            student=target,
            proposal=outside_proposal,
        )

        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_full",
                "target_id": target.pk,
                "reason": "Reinicialização integral autorizada.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["summary"]["proposals_preserved"], 1)
        self.assertEqual(preview.json()["summary"]["proposals_removed"], 1)
        self.assertEqual(
            preview.json()["summary"]["third_party_votes_preserved"],
            2,
        )
        self.assertEqual(
            preview.json()["summary"]["proposal_images_preserved"],
            2,
        )

        confirmed = self.post_json(
            self.confirm_url,
            {"challenge_token": preview.json()["challenge_token"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        self.assertTrue(ShirtProposal.objects.filter(pk=retained_proposal.pk).exists())
        self.assertFalse(ShirtProposal.objects.filter(pk=removed_proposal.pk).exists())
        self.assertTrue(ShirtProposal.objects.filter(pk=outside_proposal.pk).exists())
        self.assertEqual(Vote.objects.filter(
            proposal=retained_proposal,
        ).count(), 2)
        self.assertFalse(Vote.objects.filter(pk=target_vote.pk).exists())
        self.assertTrue(storage.exists(retained_image))
        self.assertTrue(storage.exists(shared_image))
        self.assertFalse(MaintenanceFileCleanup.objects.filter(
            storage_name=shared_image,
        ).exists())

    def test_deferred_image_cleanup_skips_files_still_referenced_by_proposals(self):
        author = self.create_user("cleanup_image_author")
        storage = ShirtProposal._meta.get_field("image").storage
        image_name = storage.save(
            "shirt_proposals/shared-cleanup.png",
            ContentFile(b"shared-image"),
        )
        proposal = self.create_proposal(author, image=image_name)
        cleanup = MaintenanceFileCleanup.objects.create(
            storage_name=image_name,
        )

        failures = _process_maintenance_file_cleanup()

        self.assertEqual(failures, 0)
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertTrue(storage.exists(image_name))
        self.assertFalse(MaintenanceFileCleanup.objects.filter(pk=cleanup.pk).exists())

    def test_result_snapshots_reject_instance_and_queryset_mutations(self):
        snapshot = ElectionResultSnapshot.objects.create(
            sequence=1,
            payload={"total_votes": 1},
            image_manifest={},
            payload_sha256="a" * 64,
            state_sha256="b" * 64,
            trigger="test",
        )

        snapshot.payload = {"total_votes": 99}
        with self.assertRaises(ValidationError):
            snapshot.save()
        with self.assertRaises(ValidationError):
            snapshot.delete()
        with self.assertRaises(ValidationError):
            ElectionResultSnapshot.objects.filter(pk=snapshot.pk).update(
                payload={"total_votes": 0},
            )
        with self.assertRaises(ValidationError):
            ElectionResultSnapshot.objects.filter(pk=snapshot.pk).delete()
        with self.assertRaises(ValidationError):
            ElectionResultSnapshot.objects.bulk_update(
                [snapshot],
                ["payload"],
            )
        with self.assertRaises(ValidationError):
            ElectionResultSnapshot.objects.bulk_create([
                ElectionResultSnapshot(
                    sequence=2,
                    payload={},
                    image_manifest={},
                    payload_sha256="c" * 64,
                    state_sha256="d" * 64,
                    trigger="test",
                )
            ])

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.payload, {"total_votes": 1})
        self.assertEqual(ElectionResultSnapshot.objects.count(), 1)

    def test_reset_preview_is_invalidated_when_participation_changes(self):
        target = self.create_user("stale_reset_target")
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_full",
                "target_id": target.pk,
                "reason": "Reinicialização integral autorizada.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        PreferenceResponse.objects.create(
            student=target,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )

        response = self.post_json(
            self.confirm_url,
            {"challenge_token": preview.json()["challenge_token"]},
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())
        self.assertTrue(PreferenceResponse.objects.filter(student=target).exists())
        self.assertFalse(MaintenanceAudit.objects.exists())

    def test_destructive_maintenance_previews_are_available_without_execution(self):
        target = self.create_user("preview_delete_target")
        delete_account = self.post_json(
            self.preview_url,
            {
                "action": "delete_user",
                "target_id": target.pk,
                "reason": "Revisão administrativa.",
            },
        )
        self.assertEqual(delete_account.status_code, 200, delete_account.content)
        self.assertEqual(delete_account.json()["target"]["id"], target.pk)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())

        proposal = self.create_proposal(target, ShirtProposal.Status.APPROVED)
        delete_proposal = self.post_json(
            self.preview_url,
            {
                "action": "delete_proposal",
                "target_id": proposal.pk,
                "reason": "Revisão administrativa.",
            },
        )
        self.assertEqual(delete_proposal.status_code, 200, delete_proposal.content)
        self.assertEqual(delete_proposal.json()["target"]["status"], "APPROVED")
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())

        reset_election = self.post_json(
            self.preview_url,
            {
                "action": "reset_election",
                "target_id": None,
                "reason": "Revisão administrativa.",
            },
        )
        self.assertEqual(reset_election.status_code, 200, reset_election.content)
        self.assertIn("identidades", reset_election.json()["warning"])
        self.assertTrue(User.objects.filter(pk=target.pk).exists())
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())

    def test_account_deletion_preserves_identity_and_audit_history(self):
        target = self.create_user("delete_account_target")
        voter = self.create_user("delete_account_voter")
        response = PreferenceResponse.objects.create(
            student=target,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        proposal = self.create_proposal(target)
        identity = ParticipationIdentity.objects.get(
            user=target,
            is_current=True,
        )
        vote = Vote.objects.create(student=voter, proposal=proposal)
        event = ParticipationAuditEvent.objects.create(
            participation_type=ParticipationAuditEvent.ParticipationType.PROPOSAL,
            participation_id=proposal.pk,
            participation_identity=identity,
            actor=self.admin,
            actor_identifier=self.admin.pk,
            action=ParticipationAuditEvent.Action.INVALIDATE,
            reason="Registro histórico.",
        )
        account_event = AccountAdminAudit.objects.create(
            actor=self.admin,
            actor_identifier=self.admin.pk,
            target=target,
            target_identifier=target.pk,
            action=AccountAdminAudit.Action.ROLE_CHANGED,
            reason="Registro de auditoria.",
        )

        preview = self.post_json(
            self.preview_url,
            {
                "action": "delete_user",
                "target_id": target.pk,
                "reason": "Conta duplicada confirmada.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(
            preview.json()["summary"]["participation_identities_preserved"],
            1,
        )
        result = self.post_json(
            self.confirm_url,
            {"challenge_token": preview.json()["challenge_token"]},
        )

        self.assertEqual(result.status_code, 200, result.content)
        self.assertFalse(User.objects.filter(pk=target.pk).exists())
        self.assertFalse(PreferenceResponse.objects.filter(pk=response.pk).exists())
        self.assertFalse(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertFalse(Vote.objects.filter(pk=vote.pk).exists())
        identity.refresh_from_db()
        event.refresh_from_db()
        account_event.refresh_from_db()
        self.assertIsNone(identity.user_id)
        self.assertTrue(ParticipationAuditEvent.objects.filter(pk=event.pk).exists())
        self.assertEqual(event.participation_identity_id, identity.pk)
        self.assertIsNone(account_event.target_id)
        self.assertEqual(account_event.target_identifier, target.pk)
        self.assertEqual(result.json()["backup"]["integrity"], "ok")
        audit = MaintenanceAudit.objects.get(
            action=MaintenanceAudit.Action.DELETE_USER,
        )
        self.assertEqual(audit.actor_id, self.admin.pk)
        self.assertEqual(audit.target_identifier, target.pk)
        self.assertEqual(audit.reason, "Conta duplicada confirmada.")
        self.assertEqual(audit.result, "completed")

    def test_student_admin_cannot_delete_admin_and_last_admin_is_protected(self):
        student_admin = self.create_user(
            "delete_guard_student_admin",
            UserProfile.Role.STUDENT_ADMIN,
        )
        target_admin = self.create_user(
            "delete_guard_target_admin",
            UserProfile.Role.ADMIN,
        )
        self.client.force_login(student_admin)
        denied = self.post_json(
            self.preview_url,
            {
                "action": "delete_user",
                "target_id": target_admin.pk,
                "reason": "Tentativa não permitida.",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(User.objects.filter(pk=target_admin.pk).exists())

        self.client.force_login(self.admin)
        last_admin = self.post_json(
            self.preview_url,
            {
                "action": "delete_user",
                "target_id": self.admin.pk,
                "reason": "Tentativa de excluir a própria conta.",
            },
        )
        self.assertEqual(last_admin.status_code, 400)
        self.assertEqual(
            last_admin.json()["fields"]["form"],
            ["Não é possível excluir a própria conta."],
        )
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
        from .views import _check_account_deletion_allowed

        with patch("election.views.User.objects.filter") as filter_users:
            filter_users.return_value.exclude.return_value.count.return_value = 0
            with self.assertRaisesMessage(
                ValidationError,
                "Não é possível excluir a última conta administrativa ativa.",
            ):
                _check_account_deletion_allowed(self.admin, self.admin)

    def test_approved_proposal_deletion_preserves_snapshots_and_audits(self):
        author = self.create_user("delete_proposal_author")
        voter = self.create_user("delete_proposal_voter")
        proposal = self.create_proposal(author, ShirtProposal.Status.APPROVED)
        vote = Vote.objects.create(student=voter, proposal=proposal)
        identity = ParticipationIdentity.objects.get(
            user=author,
            is_current=True,
        )
        event = ParticipationAuditEvent.objects.create(
            participation_type=ParticipationAuditEvent.ParticipationType.PROPOSAL,
            participation_id=proposal.pk,
            participation_identity=identity,
            actor=self.admin,
            actor_identifier=self.admin.pk,
            action=ParticipationAuditEvent.Action.INVALIDATE,
            reason="Auditoria a preservar.",
        )
        snapshot = ElectionResultSnapshot.objects.create(
            sequence=1,
            payload={"total_votes": 1, "proposal_id": proposal.pk},
            image_manifest={},
            payload_sha256="a" * 64,
            state_sha256="b" * 64,
            trigger="test",
        )

        preview = self.post_json(
            self.preview_url,
            {
                "action": "delete_proposal",
                "target_id": proposal.pk,
                "reason": "Proposta removida a pedido.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(preview.json()["target"]["status"], "APPROVED")
        self.assertEqual(preview.json()["summary"]["votes"], 1)
        self.assertIn("Snapshots publicados", preview.json()["warning"])
        result = self.post_json(
            self.confirm_url,
            {"challenge_token": preview.json()["challenge_token"]},
        )

        self.assertEqual(result.status_code, 200, result.content)
        self.assertFalse(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertFalse(Vote.objects.filter(pk=vote.pk).exists())
        self.assertTrue(ElectionResultSnapshot.objects.filter(pk=snapshot.pk).exists())
        self.assertTrue(ParticipationAuditEvent.objects.filter(pk=event.pk).exists())
        audit = MaintenanceAudit.objects.get(
            action=MaintenanceAudit.Action.DELETE_PROPOSAL,
        )
        self.assertEqual(audit.target_identifier, proposal.pk)
        self.assertEqual(audit.reason, "Proposta removida a pedido.")

    def test_election_reset_preserves_identity_audits_and_published_snapshots(self):
        student = self.create_user("election_reset_student")
        proposal = self.create_proposal(student)
        identity = ParticipationIdentity.objects.get(
            user=student,
            is_current=True,
        )
        response = PreferenceResponse.objects.create(
            student=student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        vote = Vote.objects.create(student=student, proposal=proposal)
        event = ParticipationAuditEvent.objects.create(
            participation_type=ParticipationAuditEvent.ParticipationType.VOTE,
            participation_id=vote.pk,
            participation_identity=identity,
            actor=self.admin,
            actor_identifier=self.admin.pk,
            action=ParticipationAuditEvent.Action.INVALIDATE,
            reason="Auditoria a preservar.",
        )
        snapshot = ElectionResultSnapshot.objects.create(
            sequence=1,
            payload={"total_votes": 1},
            image_manifest={},
            payload_sha256="c" * 64,
            state_sha256="d" * 64,
            trigger="test",
        )
        settings_row = ElectionSettings.objects.create(
            survey_open=True,
            proposal_submission_open=False,
            voting_open=False,
            results_release_authorized=True,
            compatibility_weight=75,
        )
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_election",
                "target_id": None,
                "reason": "Encerramento autorizado do ciclo.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        token = preview.json()["challenge_token"]

        wrong_phrase = self.post_json(
            self.confirm_url,
            {
                "challenge_token": token,
                "confirmation_phrase": "reiniciar eleição",
            },
        )
        self.assertEqual(wrong_phrase.status_code, 400)
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())

        result = self.post_json(
            self.confirm_url,
            {
                "challenge_token": token,
                "confirmation_phrase": "REINICIAR ELEIÇÃO",
            },
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertFalse(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertFalse(PreferenceResponse.objects.filter(pk=response.pk).exists())
        self.assertFalse(Vote.objects.filter(pk=vote.pk).exists())
        self.assertTrue(User.objects.filter(pk=student.pk).exists())
        self.assertTrue(ParticipationIdentity.objects.filter(pk=identity.pk).exists())
        self.assertTrue(ParticipationAuditEvent.objects.filter(pk=event.pk).exists())
        self.assertTrue(ElectionResultSnapshot.objects.filter(pk=snapshot.pk).exists())
        settings_row.refresh_from_db()
        self.assertFalse(settings_row.survey_open)
        self.assertTrue(settings_row.proposal_submission_open)
        self.assertFalse(settings_row.voting_open)
        self.assertFalse(settings_row.results_release_authorized)
        self.assertEqual(settings_row.compatibility_weight, 50)
        self.assertEqual(result.json()["backup"]["integrity"], "ok")
        audit = MaintenanceAudit.objects.get(
            action=MaintenanceAudit.Action.RESET_ELECTION,
        )
        self.assertEqual(audit.result, "completed")
        self.assertEqual(audit.reason, "Encerramento autorizado do ciclo.")

    def test_election_reset_is_blocked_if_verified_backup_cannot_be_created(self):
        proposal = self.create_proposal(self.create_user("backup_blocked_author"))
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_election",
                "target_id": None,
                "reason": "Teste de bloqueio de backup.",
            },
        )
        from .views import MaintenanceBackupError

        with patch(
            "election.views._create_verified_database_backup",
            side_effect=MaintenanceBackupError("backup unavailable"),
        ):
            result = self.post_json(
                self.confirm_url,
                {
                    "challenge_token": preview.json()["challenge_token"],
                    "confirmation_phrase": "REINICIAR ELEIÇÃO",
                },
            )

        self.assertEqual(result.status_code, 503)
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertEqual(
            MaintenanceAudit.objects.get(
                action=MaintenanceAudit.Action.RESET_ELECTION,
            ).result,
            "backup_failed",
        )

    def test_proposal_deletion_rolls_back_if_operation_fails_mid_transaction(self):
        author = self.create_user("rollback_delete_author")
        voter = self.create_user("rollback_delete_voter")
        proposal = self.create_proposal(author, ShirtProposal.Status.APPROVED)
        vote = Vote.objects.create(student=voter, proposal=proposal)
        preview = self.post_json(
            self.preview_url,
            {
                "action": "delete_proposal",
                "target_id": proposal.pk,
                "reason": "Teste de atomicidade.",
            },
        )
        self.assertEqual(preview.status_code, 200, preview.content)
        from .views import _apply_maintenance_action

        def apply_then_fail(*args):
            _apply_maintenance_action(*args)
            raise DatabaseError("Falha simulada após remover a proposta.")

        with (
            patch(
                "election.views._apply_maintenance_action",
                side_effect=apply_then_fail,
            ),
            patch("election.views.logger.exception"),
        ):
            result = self.post_json(
                self.confirm_url,
                {"challenge_token": preview.json()["challenge_token"]},
            )

        self.assertEqual(result.status_code, 500)
        self.assertTrue(ShirtProposal.objects.filter(pk=proposal.pk).exists())
        self.assertTrue(Vote.objects.filter(pk=vote.pk).exists())
        self.assertTrue(
            MaintenanceAudit.objects.filter(
                action=MaintenanceAudit.Action.DELETE_PROPOSAL,
                result="failed",
                reason="Teste de atomicidade.",
            ).exists()
        )

    def test_reset_confirmation_cannot_be_replayed(self):
        target = self.create_user("replay_reset_target")
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_basic",
                "target_id": target.pk,
                "reason": "Reinicialização autorizada.",
            },
        )
        token = preview.json()["challenge_token"]
        first = self.post_json(self.confirm_url, {"challenge_token": token})
        replay = self.post_json(self.confirm_url, {"challenge_token": token})

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(
            ParticipationIdentity.objects.filter(original_user_id=target.pk).count(),
            2,
        )
        self.assertEqual(MaintenanceAudit.objects.count(), 1)

    def test_reset_database_failure_rolls_back_participation_and_audit(self):
        target = self.create_user("rollback_reset_target")
        response = PreferenceResponse.objects.create(
            student=target,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_basic",
                "target_id": target.pk,
                "reason": "Teste de rollback.",
            },
        )
        from .views import _apply_maintenance_action

        def apply_then_fail(*args):
            _apply_maintenance_action(*args)
            raise DatabaseError("Falha simulada após aplicar o reset.")

        with (
            patch(
                "election.views._apply_maintenance_action",
                side_effect=apply_then_fail,
            ),
            patch("election.views.logger.exception"),
        ):
            failed = self.post_json(
                self.confirm_url,
                {"challenge_token": preview.json()["challenge_token"]},
            )

        self.assertEqual(failed.status_code, 500)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())
        self.assertTrue(PreferenceResponse.objects.filter(pk=response.pk).exists())
        self.assertEqual(
            ParticipationIdentity.objects.filter(
                user=target,
                is_current=True,
            ).count(),
            1,
        )
        audit = MaintenanceAudit.objects.get(
            action=MaintenanceAudit.Action.RESET_USER_BASIC,
        )
        self.assertEqual(audit.result, "failed")
        self.assertFalse(AccountAdminAudit.objects.filter(
            target=target,
            action=AccountAdminAudit.Action.ACCOUNT_RESET,
        ).exists())

    def test_full_reset_file_cleanup_failure_is_reported_and_retryable(self):
        target = self.create_user("cleanup_reset_target")
        storage = ShirtProposal._meta.get_field("image").storage
        image_name = storage.save(
            "shirt_proposals/reset-cleanup.png",
            ContentFile(b"cleanup-image"),
        )
        self.create_proposal(target, image=image_name)
        preview = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_full",
                "target_id": target.pk,
                "reason": "Limpeza de imagem autorizada.",
            },
        )

        with (
            patch.object(storage, "delete", side_effect=PermissionError),
            patch("election.views.logger.exception"),
        ):
            result = self.post_json(
                self.confirm_url,
                {"challenge_token": preview.json()["challenge_token"]},
            )

        self.assertEqual(result.status_code, 200, result.content)
        self.assertTrue(result.json()["file_cleanup_pending"])
        cleanup = MaintenanceFileCleanup.objects.get(storage_name=image_name)
        self.assertTrue(storage.exists(image_name))

        self.assertEqual(_process_maintenance_file_cleanup(), 0)
        self.assertFalse(MaintenanceFileCleanup.objects.filter(pk=cleanup.pk).exists())
        self.assertFalse(storage.exists(image_name))

    def test_invalidation_requires_reason_and_keeps_audited_record(self):
        student = self.create_user("invalidation_student")
        response = PreferenceResponse.objects.create(
            student=student,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        invalidation_url = (
            f"/api/election/admin/participations/preference_response/"
            f"{response.pk}/invalidate/"
        )
        missing_reason = self.post_json(invalidation_url, {})
        self.assertEqual(missing_reason.status_code, 400)
        self.assertFalse(PreferenceResponse.objects.get(pk=response.pk).is_invalidated)

        invalidated = self.post_json(
            invalidation_url,
            {"reason": "Participação comprovadamente indevida"},
        )
        self.assertEqual(invalidated.status_code, 200, invalidated.content)
        response.refresh_from_db()
        self.assertTrue(response.is_invalidated)
        self.assertEqual(
            ParticipationAuditEvent.objects.filter(
                participation_id=response.pk,
                action=ParticipationAuditEvent.Action.INVALIDATE,
                actor=self.admin,
            ).get().reason,
            "Participação comprovadamente indevida",
        )

        restored = self.post_json(
            f"/api/election/admin/participations/preference_response/"
            f"{response.pk}/restore/",
            {"reason": "Revisão administrativa"},
        )
        self.assertEqual(restored.status_code, 200, restored.content)
        response.refresh_from_db()
        self.assertFalse(response.is_invalidated)
        self.assertEqual(
            ParticipationAuditEvent.objects.filter(
                participation_id=response.pk,
            ).count(),
            2,
        )

    def test_published_results_are_versioned_when_a_vote_is_invalidated(self):
        student = self.create_user("snapshot_student")
        author = self.create_user("snapshot_author")
        proposal = self.create_proposal(author)
        Vote.objects.create(student=student, proposal=proposal)
        ElectionSettings.objects.create(
            voting_open=False,
            results_release_authorized=True,
        )
        original = create_result_snapshot(self.admin, "initial_release")
        original_payload = original.payload.copy()
        original_hash = original.payload_sha256
        vote = Vote.objects.get(student=student)

        invalidated = self.post_json(
            f"/api/election/admin/participations/vote/{vote.pk}/invalidate/",
            {"reason": "Voto comprovadamente indevido"},
        )

        self.assertEqual(invalidated.status_code, 200, invalidated.content)
        original.refresh_from_db()
        self.assertEqual(original.payload, original_payload)
        self.assertEqual(original.payload_sha256, original_hash)
        snapshots = list(ElectionResultSnapshot.objects.order_by("sequence"))
        self.assertEqual([snapshot.sequence for snapshot in snapshots], [1, 2])
        self.assertEqual(snapshots[0].payload["total_votes"], 1)
        self.assertEqual(
            snapshots[1].trigger,
            "participation_invalidation_after",
        )
        self.assertEqual(snapshots[1].payload["total_votes"], 0)

    def test_replacement_vote_requires_audited_authorization(self):
        student = self.create_user("replacement_student")
        proposal_author = self.create_user("replacement_author")
        first_proposal = self.create_proposal(proposal_author)
        second_proposal = self.create_proposal(proposal_author)
        ElectionSettings.objects.create(
            voting_open=True,
            voting_started_at=timezone.now(),
        )
        first_vote = cast_vote(student, first_proposal.pk)
        invalidated = self.post_json(
            f"/api/election/admin/participations/vote/{first_vote.pk}/invalidate/",
            {"reason": "Voto comprovadamente indevido"},
        )
        self.assertEqual(invalidated.status_code, 200, invalidated.content)

        student_client = Client()
        student_client.force_login(student)
        denied = student_client.post(
            "/api/election/student/vote/",
            data=json.dumps({"proposal_id": second_proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(denied.status_code, 400)

        authorization = self.post_json(
            f"/api/election/admin/users/{student.pk}/vote-replacement/",
            {"reason": "Novo voto autorizado após investigação"},
        )
        self.assertEqual(authorization.status_code, 200, authorization.content)
        granted = VoteReplacementAuthorization.objects.get(
            participation_identity=first_vote.participation_identity,
        )
        self.assertEqual(granted.actor_id, self.admin.pk)
        self.assertEqual(
            ParticipationAuditEvent.objects.filter(
                participation_id=first_vote.pk,
                action=(
                    ParticipationAuditEvent.Action.AUTHORIZE_REPLACEMENT_VOTE
                ),
                actor=self.admin,
            ).count(),
            1,
        )

        accepted = student_client.post(
            "/api/election/student/vote/",
            data=json.dumps({"proposal_id": second_proposal.pk}),
            content_type="application/json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.content)
        valid_votes = Vote.objects.filter(
            participation_identity=first_vote.participation_identity,
            is_invalidated=False,
        )
        self.assertEqual(valid_votes.count(), 1)
        self.assertEqual(
            VoteReplacementAuthorization.objects.get(pk=granted.pk).consumed_at,
            valid_votes.get().created_at,
        )
        self.assertEqual(Vote.objects.filter(
            participation_identity=first_vote.participation_identity,
        ).count(), 2)

    def test_student_admin_cannot_reset_admin_or_invalidate_participation(self):
        student_admin = self.create_user(
            "integrity_student_admin",
            UserProfile.Role.STUDENT_ADMIN,
        )
        target = self.create_user("protected_admin", UserProfile.Role.ADMIN)
        response = PreferenceResponse.objects.create(
            student=target,
            base_color="BLACK",
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        self.client.force_login(student_admin)
        reset = self.post_json(
            self.preview_url,
            {
                "action": "reset_user_basic",
                "target_id": target.pk,
                "reason": "Teste de autorização",
            },
        )
        self.assertEqual(reset.status_code, 403)
        invalidation = self.post_json(
            f"/api/election/admin/participations/preference_response/"
            f"{response.pk}/invalidate/",
            {"reason": "Teste de autorização"},
        )
        self.assertEqual(invalidation.status_code, 403)
        target.refresh_from_db()
        self.assertTrue(target.is_active)
        self.assertEqual(target.profile.role, UserProfile.Role.ADMIN)
        self.assertFalse(response.is_invalidated)
