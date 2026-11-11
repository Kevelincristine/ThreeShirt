import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
from io import StringIO

from django.apps import apps
from django.conf import settings
from django.core.management import BaseCommand, CommandError, call_command
from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.migrations.loader import MigrationLoader
from django.core.files.base import ContentFile
from urllib.parse import quote

from election.models import ElectionResultSnapshot, ShirtProposal
from election.storage import PrivateProposalStorage


EXCLUDED_MODELS = {
    "contenttypes.contenttype",
    "auth.permission",
    "sessions.session",
}


class Command(BaseCommand):
    help = (
        "Transfer data from an offline SQLite copy to an empty PostgreSQL "
        "database after separately applying migrations."
    )

    def add_arguments(self, parser):
        parser.add_argument("--source-copy", required=True, type=Path)
        parser.add_argument("--media-root", required=True, type=Path)
        parser.add_argument("--private-media-root", required=True, type=Path)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--confirm-transfer", action="store_true")
        parser.add_argument("--confirm-storage-write", action="store_true")

    def handle(self, *args, **options):
        source_path = options["source_copy"].resolve()
        original_path = (Path(settings.BASE_DIR) / "db.sqlite3").resolve()
        if source_path == original_path:
            raise CommandError(
                "Use a separately created, verified SQLite copy as the source."
            )
        if not source_path.is_file():
            raise CommandError("The SQLite source copy is not accessible.")
        if connections[DEFAULT_DB_ALIAS].vendor != "postgresql":
            raise CommandError("The target database must be PostgreSQL.")
        if not options["dry_run"] and not settings.SUPABASE_STORAGE_ENABLED:
            raise CommandError(
                "Supabase Storage must be configured before transferring images."
            )
        if not options["dry_run"] and not options["confirm_transfer"]:
            raise CommandError(
                "Pass --confirm-transfer after verifying an isolated target."
            )
        if not options["dry_run"] and not options["confirm_storage_write"]:
            raise CommandError(
                "Pass --confirm-storage-write to authorize image uploads."
            )

        source_name = (
            f"file:{quote(source_path.as_posix(), safe='/:')}?mode=ro"
        )
        connections.databases["sqlite_source"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": source_name,
            "OPTIONS": {"uri": True, "transaction_mode": "DEFERRED"},
        }
        source_connection = connections["sqlite_source"]
        try:
            source_connection.ensure_connection()
            self._require_applied_migrations(source_connection)
            self._require_applied_migrations(
                connections[DEFAULT_DB_ALIAS]
            )
            source_counts = self._model_counts("sqlite_source")
            self._require_empty_target()
            assets = self._source_assets(
                "sqlite_source",
                options["media_root"].resolve(),
                options["private_media_root"].resolve(),
            )
            fixture = self._dump_fixture("sqlite_source")
            fixture_digest = self._fixture_digest(fixture)

            if options["dry_run"]:
                self.stdout.write(
                    self.style.SUCCESS(
                        "Preflight passed: "
                        f"{sum(source_counts.values())} records, "
                        f"{len(assets)} referenced files, "
                        f"fixture SHA-256 {fixture_digest}."
                    )
                )
                return

            self._copy_assets(assets)
            with transaction.atomic(using=DEFAULT_DB_ALIAS):
                self._load_fixture(fixture)
                self._reset_sequences()
                target_counts = self._model_counts(DEFAULT_DB_ALIAS)
                if target_counts != source_counts:
                    raise CommandError(
                        "Source and target record counts do not match."
                    )
                target_fixture = self._dump_fixture(DEFAULT_DB_ALIAS)
                target_digest = self._fixture_digest(target_fixture)
                if target_digest != fixture_digest:
                    raise CommandError(
                        "Source and target fixture digests do not match."
                    )

            self.stdout.write(
                self.style.SUCCESS(
                    "Transfer validated: "
                    f"{sum(source_counts.values())} records, "
                    f"{len(assets)} referenced files, "
                    f"fixture SHA-256 {fixture_digest}."
                )
            )
        finally:
            source_connection.close()
            connections["sqlite_source"].close()
            connections.databases.pop("sqlite_source", None)

    @staticmethod
    def _require_applied_migrations(database_connection):
        loader = MigrationLoader(database_connection, ignore_no_migrations=True)
        conflicts = loader.detect_conflicts()
        if conflicts:
            raise CommandError(
                "Resolve conflicting migrations before transferring data."
            )
        missing = set(loader.graph.leaf_nodes()) - loader.applied_migrations
        if missing:
            raise CommandError(
                "Apply all migrations to the source copy and empty target "
                "before transferring data."
            )

    @staticmethod
    def _included_models():
        return [
            model
            for model in apps.get_models()
            if model._meta.managed
            and not model._meta.proxy
            and model._meta.label_lower not in EXCLUDED_MODELS
        ]

    def _model_counts(self, database_alias):
        return {
            model._meta.label_lower: model._base_manager.using(
                database_alias
            ).count()
            for model in self._included_models()
        }

    def _require_empty_target(self):
        for model in self._included_models():
            if model._base_manager.using(DEFAULT_DB_ALIAS).exists():
                raise CommandError(
                    "The PostgreSQL target must be empty before transfer."
                )

    @staticmethod
    def _dump_fixture(database_alias):
        output = StringIO()
        call_command(
            "dumpdata",
            database=database_alias,
            all=True,
            natural_foreign=True,
            exclude=sorted(EXCLUDED_MODELS),
            verbosity=0,
            stdout=output,
        )
        return output.getvalue()

    @staticmethod
    def _fixture_digest(fixture):
        canonical_fixture = json.dumps(
            json.loads(fixture),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(
            canonical_fixture.encode("utf-8")
        ).hexdigest()

    def _source_assets(self, database_alias, media_root, private_root):
        assets = {}
        for proposal in ShirtProposal.objects.using(database_alias).exclude(
            image=""
        ).iterator():
            self._register_source_asset(
                assets,
                "",
                proposal.image.name,
                (private_root, media_root),
            )

        for snapshot in ElectionResultSnapshot.objects.using(
            database_alias
        ).iterator():
            manifest = snapshot.image_manifest
            if not isinstance(manifest, dict):
                raise CommandError(
                    "A result snapshot contains an invalid image manifest."
                )
            for entry in manifest.values():
                if not isinstance(entry, dict):
                    raise CommandError(
                        "A result snapshot contains an invalid image entry."
                    )
                name = entry.get("name")
                expected_digest = entry.get("sha256")
                if (
                    not isinstance(name, str)
                    or not isinstance(expected_digest, str)
                    or len(expected_digest) != 64
                ):
                    raise CommandError(
                        "A result snapshot contains invalid image metadata."
                    )
                self._register_source_asset(
                    assets,
                    "election_result_snapshots",
                    name,
                    (
                        private_root / "election_result_snapshots",
                        media_root / "election_result_snapshots",
                    ),
                    expected_digest,
                )
        return list(assets.values())

    @staticmethod
    def _safe_relative_path(name):
        raw_name = str(name)
        path = PurePosixPath(raw_name.replace("\\", "/"))
        if (
            path.is_absolute()
            or PureWindowsPath(raw_name).drive
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise CommandError("A referenced file has an invalid path.")
        return path

    def _register_source_asset(
        self,
        assets,
        prefix,
        name,
        roots,
        expected_digest=None,
    ):
        relative_path = self._safe_relative_path(name)
        matches = []
        for root in roots:
            resolved_root = root.resolve()
            candidate = root.joinpath(*relative_path.parts).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                continue
            if candidate.is_file():
                matches.append(candidate)
        if not matches:
            raise CommandError(
                "A referenced image is missing from the source media copy."
            )
        digests = {
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in matches
        }
        if len(digests) != 1:
            raise CommandError(
                "Source media roots contain conflicting copies of an image."
            )
        digest = digests.pop()
        if expected_digest is not None and digest != expected_digest:
            raise CommandError(
                "A snapshot image does not match its stored SHA-256 digest."
            )
        key = (prefix, relative_path.as_posix())
        previous = assets.get(key)
        if previous is not None and previous["sha256"] != digest:
            raise CommandError(
                "Multiple records reference conflicting image content."
            )
        assets[key] = {
            "prefix": prefix,
            "name": relative_path.as_posix(),
            "path": matches[0],
            "sha256": digest,
        }

    @staticmethod
    def _copy_assets(assets):
        for asset in assets:
            storage = PrivateProposalStorage(prefix=asset["prefix"])
            name = asset["name"]
            if storage.exists(name):
                with storage.open(name, "rb") as existing:
                    existing_digest = hashlib.sha256(
                        existing.read()
                    ).hexdigest()
                if existing_digest != asset["sha256"]:
                    raise CommandError(
                        "The target Storage already contains conflicting "
                        "content for a referenced image."
                    )
                continue

            content = ContentFile(asset["path"].read_bytes(), name=name)
            saved_name = storage.save(name, content)
            if saved_name != name:
                raise CommandError(
                    "Storage changed a referenced image path during transfer."
                )
            with storage.open(name, "rb") as uploaded:
                uploaded_digest = hashlib.sha256(uploaded.read()).hexdigest()
            if uploaded_digest != asset["sha256"]:
                raise CommandError(
                    "An uploaded image failed SHA-256 verification."
                )

    @staticmethod
    def _load_fixture(fixture):
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as fixture_file:
            fixture_file.write(fixture)
            fixture_path = Path(fixture_file.name)
        try:
            call_command(
                "loaddata",
                str(fixture_path),
                database=DEFAULT_DB_ALIAS,
                verbosity=0,
            )
        finally:
            fixture_path.unlink(missing_ok=True)

    @staticmethod
    def _reset_sequences():
        models = Command._included_models()
        statements = connections[DEFAULT_DB_ALIAS].ops.sequence_reset_sql(
            no_style(),
            models,
        )
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
