
import re
import threading
from datetime import timedelta
from unittest.mock import patch
from accounts.admin import three_shirt_admin
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core import mail
from django.db import OperationalError
from django.db.models.query import QuerySet
from django.test import (
    Client,
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone
from django.contrib.sessions.middleware import SessionMiddleware

from . import views
from .models import EmailLoginCode, LoginRequestRateBucket

User = get_user_model()

TEST_EMAIL = "estudante@escola.com"
TEST_PASSWORD = "senha-de-teste"


@override_settings(
    AUTHORIZED_LOGIN_EMAILS=[TEST_EMAIL],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LoginTests(TestCase):
    def setUp(self):
        password_patcher = patch(
            "accounts.views.make_password",
            return_value="test-password-hash",
        )
        password_patcher.start()
        self.addCleanup(password_patcher.stop)
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="estudante1",
            email=TEST_EMAIL,
            password=TEST_PASSWORD,
        )

    def make_request(
        self,
        view,
        data,
        remote_addr="127.0.0.1",
        forwarded_for=None,
    ):
        request = self.factory.post("/", data=data)
        request.META["REMOTE_ADDR"] = remote_addr
        if forwarded_for is not None:
            request.META["HTTP_X_FORWARDED_FOR"] = forwarded_for
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
                remote_addr=f"192.0.2.{_ + 1}",
            )

        self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
            remote_addr="192.0.2.4",
        )

        self.assertEqual(EmailLoginCode.objects.count(), 3)
        self.assertEqual(len(mail.outbox), 3)

    def test_request_code_is_rate_limited_by_source(self):
        emails = [f"student-{index}@escola.com" for index in range(11)]
        with override_settings(AUTHORIZED_LOGIN_EMAILS=emails):
            responses = [
                self.make_request(
                    views.request_login_code,
                    {"email": email},
                    remote_addr="198.51.100.20",
                    forwarded_for=f"203.0.113.{index + 1}",
                )
                for index, email in enumerate(emails)
            ]

        self.assertTrue(
            all(response.status_code == 200 for response in responses)
        )
        self.assertEqual(len(mail.outbox), 10)
        self.assertEqual(EmailLoginCode.objects.count(), 10)
        self.assertEqual(
            len(responses[0].content),
            len(responses[-1].content),
        )
        self.assertEqual(responses[0].content, responses[-1].content)
        active_code = self.create_code(
            code="654321",
            email=emails[0],
        )
        blocked = self.make_request(
            views.request_login_code,
            {"email": emails[0]},
            remote_addr="198.51.100.20",
        )
        active_code.refresh_from_db()

        self.assertEqual(blocked.status_code, 200)
        self.assertIsNone(active_code.used_at)
        self.assertEqual(len(mail.outbox), 10)

    def test_blocked_account_request_does_not_invalidate_active_code(self):
        for _ in range(3):
            self.make_request(
                views.request_login_code,
                {"email": TEST_EMAIL},
                remote_addr=f"192.0.2.{_ + 1}",
            )
        active_code = self.create_code(code="654321")

        response = self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
            remote_addr="192.0.2.4",
        )

        active_code.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(active_code.used_at)
        self.assertEqual(len(mail.outbox), 3)

    def test_mail_failure_preserves_existing_active_code(self):
        active_code = self.create_code(code="654321")

        with (
            patch(
                "accounts.views.send_mail",
                side_effect=OSError("simulated mail transport failure"),
            ),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            response = self.make_request(
                views.request_login_code,
                {"email": TEST_EMAIL},
            )

        active_code.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(active_code.used_at)
        self.assertEqual(
            EmailLoginCode.objects.filter(used_at__isnull=True).count(),
            1,
        )
        unauthorized = self.make_request(
            views.request_login_code,
            {"email": "unknown@escola.com"},
            remote_addr="192.0.2.40",
        )
        self.assertEqual(response.content, unauthorized.content)

    def test_unconfirmed_mail_send_preserves_existing_active_code(self):
        active_code = self.create_code(code="654321")

        with (
            patch("accounts.views.send_mail", return_value=0),
            self.assertLogs("accounts.views", level="ERROR"),
        ):
            response = self.make_request(
                views.request_login_code,
                {"email": TEST_EMAIL},
            )

        active_code.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(active_code.used_at)
        self.assertEqual(
            EmailLoginCode.objects.filter(used_at__isnull=True).count(),
            1,
        )
        unauthorized = self.make_request(
            views.request_login_code,
            {"email": "unknown@escola.com"},
            remote_addr="192.0.2.41",
        )
        self.assertEqual(response.content, unauthorized.content)

    def test_authorized_and_unauthorized_emails_have_same_public_response(self):
        authorized = self.make_request(
            views.request_login_code,
            {"email": TEST_EMAIL},
            remote_addr="192.0.2.10",
        )
        unauthorized = self.make_request(
            views.request_login_code,
            {"email": "unknown@escola.com"},
            remote_addr="192.0.2.11",
        )
        malformed = self.make_request(
            views.request_login_code,
            {"email": "not-an-email"},
            remote_addr="192.0.2.12",
        )

        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.content, unauthorized.content)
        self.assertEqual(authorized.content, malformed.content)

    def test_activation_database_failure_preserves_previous_code(self):
        active_code = self.create_code(code="654321")
        original_update = QuerySet.update

        def fail_pending_activation(queryset, **kwargs):
            if (
                queryset.model is EmailLoginCode
                and kwargs.get("used_at", "not-provided") is None
            ):
                raise OperationalError("simulated activation failure")
            return original_update(queryset, **kwargs)

        with (
            patch("accounts.views.secrets.randbelow", return_value=123456),
            patch.object(QuerySet, "update", new=fail_pending_activation),
            self.assertLogs("accounts.views", level="ERROR") as logs,
        ):
            response = self.make_request(
                views.request_login_code,
                {"email": TEST_EMAIL},
            )

        active_code.refresh_from_db()
        codes = EmailLoginCode.objects.filter(email=TEST_EMAIL)
        pending_code = codes.exclude(pk=active_code.pk).get()
        unauthorized = self.make_request(
            views.request_login_code,
            {"email": "unknown@escola.com"},
            remote_addr="192.0.2.90",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, unauthorized.content)
        self.assertIsNone(active_code.used_at)
        self.assertIsNotNone(pending_code.used_at)
        self.assertEqual(
            EmailLoginCode.objects.filter(
                email=TEST_EMAIL,
                used_at__isnull=True,
            ).count(),
            1,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("123456", mail.outbox[0].body)
        self.assertNotIn(
            "123456",
            "\n".join(logs.output),
        )
        previous_login = self.make_request(
            views.verify_login_code,
            {"email": TEST_EMAIL, "code": "654321"},
        )
        self.assertEqual(previous_login.status_code, 200)

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
    AUTHORIZED_LOGIN_EMAILS=[TEST_EMAIL],
    CSRF_TRUSTED_ORIGINS=["http://127.0.0.1:5173"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class CsrfProtectionTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.user = User.objects.create_user(
            username="estudante_csrf",
            email=TEST_EMAIL,
        )

    def get_csrf_token(self):
        response = self.client.get("/api/accounts/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertTrue(response.json()["csrfToken"])
        return response.json()["csrfToken"]

    def test_csrf_route_only_accepts_get_and_sets_cookie(self):
        token = self.get_csrf_token()
        response = self.client.post(
            "/api/accounts/csrf/",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 405)

    def test_authentication_posts_reject_missing_and_invalid_csrf(self):
        endpoints = (
            ("/api/accounts/request-code/", {"email": TEST_EMAIL}),
            (
                "/api/accounts/verify-code/",
                {"email": TEST_EMAIL, "code": "123456"},
            ),
        )

        for path, data in endpoints:
            with self.subTest(path=path, token="missing"):
                response = self.client.post(path, data)
                self.assertEqual(response.status_code, 403)

            with self.subTest(path=path, token="invalid"):
                response = self.client.post(
                    path,
                    data,
                    HTTP_X_CSRFTOKEN="invalid-token",
                )
                self.assertEqual(response.status_code, 403)

    def test_authentication_posts_accept_csrf_token(self):
        token = self.get_csrf_token()
        request_response = self.client.post(
            "/api/accounts/request-code/",
            {"email": TEST_EMAIL},
            HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN="http://127.0.0.1:5173",
        )
        self.assertEqual(request_response.status_code, 200)

        login_code = EmailLoginCode.objects.create(
            email=TEST_EMAIL,
            code_hash=make_password("123456"),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        verify_response = self.client.post(
            "/api/accounts/verify-code/",
            {"email": TEST_EMAIL, "code": "123456"},
            HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN="http://127.0.0.1:5173",
        )

        self.assertEqual(verify_response.status_code, 200)
        self.assertEqual(
            verify_response.json()["message"],
            "Login realizado com sucesso.",
        )
        self.assertTrue(self.client.session.get("_auth_user_id"))
        login_code.refresh_from_db()
        self.assertIsNotNone(login_code.used_at)

    def test_authentication_posts_reject_untrusted_origin(self):
        token = self.get_csrf_token()
        response = self.client.post(
            "/api/accounts/request-code/",
            {"email": TEST_EMAIL},
            HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN="https://untrusted.example",
        )

        self.assertEqual(response.status_code, 403)


@override_settings(
    AUTHORIZED_LOGIN_EMAILS=[TEST_EMAIL],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class LoginCodeConcurrencyTests(TransactionTestCase):
    def setUp(self):
        password_patcher = patch(
            "accounts.views.make_password",
            return_value="test-password-hash",
        )
        password_patcher.start()
        self.addCleanup(password_patcher.stop)
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="estudante_concorrente",
            email=TEST_EMAIL,
        )
        self.code = "123456"
        self.login_code = EmailLoginCode.objects.create(
            email=TEST_EMAIL,
            code_hash=make_password(self.code),
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def make_request(self, code):
        request = self.factory.post(
            "/api/accounts/verify-code/",
            {"email": TEST_EMAIL, "code": code},
        )

        class Session(dict):
            def cycle_key(self):
                self.clear()

            def flush(self):
                self.clear()

            def set_expiry(self, value):
                self["_session_expiry"] = value

        request.session = Session()
        return request

    def run_verification(self, code, results, result_lock):
        from django.db import close_old_connections, connections

        close_old_connections()
        try:
            request = self.make_request(code)
            response = views.verify_login_code(request)
            result = (
                response.status_code,
                request.session.get("_auth_user_id"),
            )
        except Exception as exc:
            result = exc
        with result_lock:
            results.append(result)
        connections["default"].close()

    def run_code_request(self, email, remote_addr, results, result_lock):
        from django.db import close_old_connections, connections

        close_old_connections()
        try:
            request = self.factory.post(
                "/api/accounts/request-code/",
                {"email": email},
                REMOTE_ADDR=remote_addr,
            )
            response = views.request_login_code(request)
            result = response.status_code
        except Exception as exc:
            result = exc
        with result_lock:
            results.append(result)
        connections["default"].close()

    def test_simultaneous_valid_verifications_consume_code_once(self):
        check_barrier = threading.Barrier(2)
        original_check_password = check_password
        results = []
        result_lock = threading.Lock()

        def synchronized_check_password(raw, encoded):
            check_barrier.wait(timeout=20)
            return original_check_password(raw, encoded)

        with patch(
            "accounts.views.check_password",
            side_effect=synchronized_check_password,
        ):
            workers = [
                threading.Thread(
                    target=self.run_verification,
                    args=(self.code, results, result_lock),
                )
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), 2)
        self.assertTrue(
            all(not isinstance(result, Exception) for result in results),
            results,
        )
        self.assertCountEqual([result[0] for result in results], [200, 400])
        self.assertEqual(
            sum(result[1] == str(self.user.pk) for result in results),
            1,
        )
        self.login_code.refresh_from_db()
        self.assertIsNotNone(self.login_code.used_at)

    def test_simultaneous_invalid_attempts_never_exceed_limit(self):
        attempt_count = 8
        attempt_barrier = threading.Barrier(attempt_count)
        reserve_attempt = views._reserve_login_attempt
        results = []
        result_lock = threading.Lock()

        def synchronized_reservation(login_code, now):
            attempt_barrier.wait(timeout=20)
            return reserve_attempt(login_code, now)

        with patch(
            "accounts.views._reserve_login_attempt",
            side_effect=synchronized_reservation,
        ):
            workers = [
                threading.Thread(
                    target=self.run_verification,
                    args=("999999", results, result_lock),
                )
                for _ in range(attempt_count)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), attempt_count)
        self.assertTrue(
            all(not isinstance(result, Exception) for result in results),
            results,
        )
        self.assertEqual(
            [result[0] for result in results].count(400),
            attempt_count,
        )
        self.login_code.refresh_from_db()
        self.assertEqual(self.login_code.attempts, 5)
        self.assertIsNotNone(self.login_code.used_at)

    def test_simultaneous_requests_respect_account_limit_across_sources(self):
        request_count = 8
        request_barrier = threading.Barrier(request_count)
        record_request = views._record_login_request
        sent_emails = []
        email_lock = threading.Lock()
        results = []
        result_lock = threading.Lock()

        def synchronized_record(email, source, now):
            request_barrier.wait(timeout=20)
            return record_request(email, source, now)

        def record_sent_email(*args, **kwargs):
            with email_lock:
                sent_emails.append(kwargs.get("recipient_list"))
            return 1

        with (
            patch(
                "accounts.views._record_login_request",
                side_effect=synchronized_record,
            ),
            patch(
                "accounts.views.send_mail",
                side_effect=record_sent_email,
            ),
        ):
            workers = [
                threading.Thread(
                    target=self.run_code_request,
                    args=(
                        TEST_EMAIL,
                        f"198.51.100.{index + 1}",
                        results,
                        result_lock,
                    ),
                )
                for index in range(request_count)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), request_count)
        self.assertTrue(
            all(not isinstance(result, Exception) for result in results),
            results,
        )
        self.assertEqual(results.count(200), request_count)
        self.assertEqual(len(sent_emails), 3)
        self.assertEqual(
            EmailLoginCode.objects.exclude(pk=self.login_code.pk).count(),
            3,
        )

    def test_simultaneous_requests_respect_source_limit_across_accounts(self):
        request_count = 12
        emails = [
            f"source-student-{index}@escola.com"
            for index in range(request_count)
        ]
        request_barrier = threading.Barrier(request_count)
        record_request = views._record_login_request
        sent_emails = []
        email_lock = threading.Lock()
        results = []
        result_lock = threading.Lock()

        def synchronized_record(email, source, now):
            request_barrier.wait(timeout=20)
            return record_request(email, source, now)

        def record_sent_email(*args, **kwargs):
            with email_lock:
                sent_emails.append(kwargs.get("recipient_list"))
            return 1

        with (
            override_settings(AUTHORIZED_LOGIN_EMAILS=emails),
            patch(
                "accounts.views._record_login_request",
                side_effect=synchronized_record,
            ),
            patch(
                "accounts.views.send_mail",
                side_effect=record_sent_email,
            ),
        ):
            workers = [
                threading.Thread(
                    target=self.run_code_request,
                    args=(
                        email,
                        "198.51.100.50",
                        results,
                        result_lock,
                    ),
                )
                for email in emails
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), request_count)
        self.assertTrue(
            all(not isinstance(result, Exception) for result in results),
            results,
        )
        self.assertEqual(results.count(200), request_count)
        self.assertEqual(len(sent_emails), 10)
        self.assertEqual(
            EmailLoginCode.objects.exclude(pk=self.login_code.pk).count(),
            10,
        )
        self.assertTrue(
            LoginRequestRateBucket.objects.filter(
                key_digest=views._rate_limit_digest(
                    "source",
                    "198.51.100.50",
                ),
            ).exists()
        )


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
