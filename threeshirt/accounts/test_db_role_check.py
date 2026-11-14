from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

User = get_user_model()
ADMIN_EMAIL = "db-role-admin@example.com"
DB_ROLE_CHECK_PATH = "/admin/db-role-check/"


@override_settings(
    ADMIN_EMAILS=[ADMIN_EMAIL],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    },
)
class DatabaseRoleCheckTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="db-role-admin",
            email=ADMIN_EMAIL,
            password="Local-test-password-42!",
            is_staff=True,
        )
        self.client.force_login(self.admin)

    def test_get_renders_form_without_querying_database(self):
        with patch("accounts.admin.connection") as database:
            response = self.client.get(DB_ROLE_CHECK_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Executar consulta")
        self.assertNotContains(response, "rolbypassrls</dt>")
        database.cursor.assert_not_called()

    def test_post_executes_only_role_query_and_displays_only_requested_values(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        csrf_client.get(DB_ROLE_CHECK_PATH)
        csrf_token = csrf_client.cookies["csrftoken"].value

        cursor = MagicMock()
        cursor.fetchone.return_value = ("app_role", False, True)
        cursor_context = MagicMock()
        cursor_context.__enter__.return_value = cursor
        expected_query = (
            "SELECT current_user, r.rolsuper, r.rolbypassrls "
            "FROM pg_roles r "
            "WHERE r.rolname = current_user;"
        )

        with patch("accounts.admin.connection") as database:
            database.vendor = "postgresql"
            database.cursor.return_value = cursor_context
            response = csrf_client.post(
                DB_ROLE_CHECK_PATH,
                HTTP_X_CSRFTOKEN=csrf_token,
            )

        self.assertEqual(response.status_code, 200)
        cursor.execute.assert_called_once_with(expected_query)
        self.assertEqual(
            response.context["result"],
            {
                "current_user": "app_role",
                "rolsuper": False,
                "rolbypassrls": True,
            },
        )
        self.assertContains(response, "app_role")
        self.assertContains(response, "rolsuper")
        self.assertContains(response, "rolbypassrls")

    def test_post_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        csrf_client.get(DB_ROLE_CHECK_PATH)

        with patch("accounts.admin.connection") as database:
            response = csrf_client.post(DB_ROLE_CHECK_PATH)

        self.assertEqual(response.status_code, 403)
        database.cursor.assert_not_called()

    def test_non_allowlisted_staff_cannot_access_route(self):
        unauthorized = User.objects.create_user(
            username="unlisted-staff",
            email="unlisted@example.com",
            password="Local-test-password-42!",
            is_staff=True,
        )
        client = Client()
        client.force_login(unauthorized)

        with patch("accounts.admin.connection") as database:
            response = client.post(DB_ROLE_CHECK_PATH)

        self.assertEqual(response.status_code, 302)
        database.cursor.assert_not_called()
