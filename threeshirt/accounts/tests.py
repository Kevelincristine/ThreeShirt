
import re
from datetime import timedelta
from unittest.mock import patch
from accounts.admin import three_shirt_admin
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from django.contrib.sessions.middleware import SessionMiddleware

from . import views
from .models import EmailLoginCode

User = get_user_model()

TEST_EMAIL = "estudante@escola.com"
TEST_PASSWORD = "senha-de-teste"


@override_settings(
    AUTHORIZED_LOGIN_EMAILS=[TEST_EMAIL],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class LoginTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="estudante1",
            email=TEST_EMAIL,
            password=TEST_PASSWORD,
        )

    def make_request(self, view, data):
        request = self.factory.post("/", data=data)
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()
        return view(request)

    def create_code(self, code="123456", **overrides):
        data = {
            "email": TEST_EMAIL,
            "code_hash": make_password(code),
            "expires_at": timezone.now() + timedelta(minutes=10),
        }
        data.update(overrides)
        return EmailLoginCode.objects.create(**data)

    def test_request_code_sends_email_to_authorized_user(self):
        response = self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [TEST_EMAIL])
        self.assertEqual(EmailLoginCode.objects.count(), 1)

    def test_request_code_does_not_store_plaintext_code(self):
        self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
        )

        login_code = EmailLoginCode.objects.get(email=TEST_EMAIL)
        self.assertNotEqual(login_code.code_hash, "123456")
        self.assertTrue(login_code.code_hash)

    def test_unauthorized_email_does_not_receive_code(self):
        response = self.make_request(
            views.request_login_code,
            {"email": "desconhecido@escola.com"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailLoginCode.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_invalid_email_does_not_create_code(self):
        response = self.make_request(
            views.request_login_code,
            {"email": "email-invalido"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailLoginCode.objects.count(), 0)

    def test_request_code_is_rate_limited(self):
        for _ in range(3):
            self.make_request(
                views.request_login_code,
                {"email": TEST_EMAIL},
            )

        self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
        )

        self.assertEqual(EmailLoginCode.objects.count(), 3)
        self.assertEqual(len(mail.outbox), 3)

    def test_wrong_code_increments_attempts(self):
        login_code = self.create_code()

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "999999"},
        )

        login_code.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(login_code.attempts, 1)

    def test_expired_code_is_rejected_and_consumed(self):
        login_code = self.create_code(
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "123456"},
        )

        login_code.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(login_code.used_at)

    def test_code_cannot_be_used_after_five_failed_attempts(self):
        login_code = self.create_code(attempts=5)

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "123456"},
        )

        login_code.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIsNotNone(login_code.used_at)

    def test_malformed_code_is_rejected(self):
        self.create_code()

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "12AB"},
        )

        self.assertEqual(response.status_code, 400)

    def test_valid_code_logs_user_in(self):
        login_code = self.create_code()

        request = self.factory.post(
            "/",
            {"email": TEST_EMAIL, "code": "123456"},
        )
        middleware = SessionMiddleware(lambda req: None)
        middleware.process_request(request)
        request.session.save()

        response = views.verify_login_code(request)

        login_code.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(login_code.used_at)
        self.assertEqual(
            request.session.get("_auth_user_id"),
            str(self.user.pk),
        )

    def test_code_cannot_be_reused(self):
        login_code = self.create_code(used_at=timezone.now())

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "123456"},
        )

        self.assertEqual(response.status_code, 400)

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        self.create_code()

        response = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "123456"},
        )

        self.assertEqual(response.status_code, 400)

@override_settings(
    ADMIN_EMAILS=["admin@escola.com"],
)
class AdminPermissionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

        self.admin_user = User.objects.create_user(
            username="admin_teste",
            email="admin@escola.com",
            password="senha-de-teste",
            is_staff=True,
            is_superuser=True,
        )

        self.student_user = User.objects.create_user(
            username="aluno_teste",
            email="aluno@escola.com",
            password="senha-de-teste",
            is_staff=True,
        )

    def test_authorized_admin_can_access_admin_site(self):
        request = self.factory.get("/admin/")
        request.user = self.admin_user

        self.assertTrue(
            three_shirt_admin.has_permission(request)
        )

    def test_student_cannot_access_admin_site(self):
        request = self.factory.get("/admin/")
        request.user = self.student_user

        self.assertFalse(
            three_shirt_admin.has_permission(request)
        )

    def test_inactive_admin_cannot_access_admin_site(self):
        self.admin_user.is_active = False
        self.admin_user.save()

        request = self.factory.get("/admin/")
        request.user = self.admin_user

        self.assertFalse(
            three_shirt_admin.has_permission(request)
        )
