from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from .models import AccountAdminAudit, AuthRateLimitBucket, UserProfile

User = get_user_model()
STUDENT_EMAIL = "student.one@gmail.com"
ADMIN_EMAIL = "admin.one@gmail.com"
INITIAL_PASSWORD = "Initial-password-58!"
NEW_PASSWORD = "Replacement-password-73!"


@override_settings(
    AUTHORIZED_LOGIN_EMAILS=[STUDENT_EMAIL, ADMIN_EMAIL],
    ADMIN_EMAILS=[ADMIN_EMAIL],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PasswordAuthenticationTests(TestCase):
    def create_user(
        self,
        email=STUDENT_EMAIL,
        password=INITIAL_PASSWORD,
        role=UserProfile.Role.STUDENT,
        is_active=True,
    ):
        user = User.objects.create_user(
            username=f"user-{User.objects.count()}",
            email=email,
            password=password,
            is_active=is_active,
        )
        UserProfile.objects.create(user=user, role=role)
        return user

    def test_pre_registered_accounts_in_all_roles_can_login_with_email_and_password(self):
        extra_email = "hybrid.one@gmail.com"
        roles = (
            (STUDENT_EMAIL, UserProfile.Role.STUDENT, "student"),
            (extra_email, UserProfile.Role.STUDENT_ADMIN, "student_admin"),
            (ADMIN_EMAIL, UserProfile.Role.ADMIN, "admin"),
        )
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[STUDENT_EMAIL, extra_email, ADMIN_EMAIL],
        ):
            for email, role, expected_role in roles:
                with self.subTest(role=role):
                    user = User.objects.create_user(
                        username=email.split("@", 1)[0],
                        email=email,
                        password=INITIAL_PASSWORD,
                    )
                    UserProfile.objects.create(user=user, role=role)
                    client = Client()
                    response = client.post(
                        "/api/accounts/login/",
                        {"email": email, "password": INITIAL_PASSWORD},
                    )
                    self.assertEqual(response.status_code, 200, response.content)
                    session = client.get("/api/accounts/session/").json()
                    self.assertTrue(session["authenticated"])
                    self.assertEqual(session["user"]["role"], expected_role)

    def test_removed_recovery_and_admin_bootstrap_routes_are_unavailable(self):
        for path in (
            "/api/accounts/password/recovery/",
            "/api/accounts/password/recovery/status/",
            "/api/accounts/password/recovery/complete/",
            "/api/accounts/admin/password-requests/",
            "/api/accounts/admin/initial-access/",
            "/api/accounts/register/",
        ):
            with self.subTest(path=path):
                response = self.client.post(path, data={})
                self.assertEqual(response.status_code, 404)
        self.assertEqual(User.objects.count(), 0)

    def test_login_errors_are_generic_for_unlisted_and_wrong_credentials(self):
        user = self.create_user()
        responses = [
            self.client.post(
                "/api/accounts/login/",
                {"email": "unknown@gmail.com", "password": INITIAL_PASSWORD},
            ),
            self.client.post(
                "/api/accounts/login/",
                {"email": STUDENT_EMAIL, "password": "wrong-password"},
            ),
        ]

        self.assertEqual([response.status_code for response in responses], [400, 400])
        self.assertEqual(
            [response.json()["error"] for response in responses],
            ["E-mail ou senha inválidos."] * 2,
        )
        self.assertFalse(self.client.get("/api/accounts/session/").json()["authenticated"])
        self.assertTrue(user.check_password(INITIAL_PASSWORD))

    def test_login_is_case_insensitive_and_blocked_users_cannot_authenticate(self):
        user = self.create_user()
        response = self.client.post(
            "/api/accounts/login/",
            {"email": STUDENT_EMAIL.upper(), "password": INITIAL_PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get("/api/accounts/session/").json()["authenticated"])

        user.is_active = False
        user.save(update_fields=["is_active"])
        self.assertFalse(self.client.get("/api/accounts/session/").json()["authenticated"])

    def test_first_access_sets_password_without_changing_role_or_creating_session(self):
        extra_email = "hybrid.one@gmail.com"
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[STUDENT_EMAIL, extra_email, ADMIN_EMAIL],
        ):
            roles = (
                (STUDENT_EMAIL, UserProfile.Role.STUDENT, "student"),
                (extra_email, UserProfile.Role.STUDENT_ADMIN, "student_admin"),
                (ADMIN_EMAIL, UserProfile.Role.ADMIN, "admin"),
            )
            for email, role, expected_role in roles:
                with self.subTest(role=role):
                    user = User.objects.create_user(
                        username=email.split("@", 1)[0],
                        email=email,
                        password=None,
                    )
                    profile = UserProfile.objects.create(user=user, role=role)
                    original_status = user.is_active
                    response = self.client.post(
                        "/api/accounts/first-access/",
                        {
                            "email": email,
                            "password": NEW_PASSWORD,
                            "password_confirmation": NEW_PASSWORD,
                        },
                    )
                    self.assertEqual(response.status_code, 200, response.content)
                    user.refresh_from_db()
                    profile.refresh_from_db()
                    self.assertTrue(user.check_password(NEW_PASSWORD))
                    self.assertEqual(profile.role, role)
                    self.assertEqual(user.is_active, original_status)
                    self.assertEqual(profile.credential_revision, 1)
                    self.assertFalse(
                        self.client.get("/api/accounts/session/").json()["authenticated"]
                    )
                    event = AccountAdminAudit.objects.get(
                        action=AccountAdminAudit.Action.FIRST_ACCESS_COMPLETED,
                        target=user,
                    )
                    self.assertIsNone(event.actor)
                    self.assertEqual(event.actor_identifier, 0)
                    self.assertEqual(event.details["result"], "completed")
                    self.assertNotIn(NEW_PASSWORD, event.reason)
                    self.assertNotIn(NEW_PASSWORD, str(event.details))

                    login_client = Client()
                    login_response = login_client.post(
                        "/api/accounts/login/",
                        {"email": email, "password": NEW_PASSWORD},
                    )
                    self.assertEqual(login_response.status_code, 200)
                    self.assertEqual(
                        login_client.get("/api/accounts/session/").json()["user"]["role"],
                        expected_role,
                    )

    def test_first_access_rejects_an_existing_password_without_changing_it(self):
        user = self.create_user()
        old_hash = user.password
        response = self.client.post(
            "/api/accounts/first-access/",
            {
                "email": STUDENT_EMAIL,
                "password": NEW_PASSWORD,
                "password_confirmation": NEW_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 409)
        user.refresh_from_db()
        self.assertEqual(user.password, old_hash)
        event = AccountAdminAudit.objects.get(
            action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
            target=user,
        )
        self.assertEqual(event.reason, "Conta já possui senha.")

    def test_first_access_rejects_missing_inactive_and_unlisted_accounts_and_audits(self):
        inactive = self.create_user(
            email="inactive@gmail.com",
            password=None,
            is_active=False,
        )
        attempts = (
            ("missing@gmail.com", 400),
            ("inactive@gmail.com", 400),
            ("unlisted@gmail.com", 400),
        )
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[STUDENT_EMAIL, ADMIN_EMAIL, inactive.email],
        ):
            for email, expected_status in attempts:
                response = self.client.post(
                    "/api/accounts/first-access/",
                    {
                        "email": email,
                        "password": NEW_PASSWORD,
                        "password_confirmation": NEW_PASSWORD,
                    },
                )
                self.assertEqual(response.status_code, expected_status)

        inactive.refresh_from_db()
        self.assertFalse(inactive.has_usable_password())
        self.assertEqual(User.objects.filter(email="missing@gmail.com").count(), 0)
        self.assertEqual(
            AccountAdminAudit.objects.filter(
                action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
            ).count(),
            3,
        )
        missing_attempt = AccountAdminAudit.objects.filter(
            target__isnull=True,
            action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
        ).first()
        self.assertIsNotNone(missing_attempt)
        self.assertEqual(missing_attempt.target_identifier, 0)
        self.assertEqual(missing_attempt.actor_identifier, 0)

    def test_first_access_requires_matching_password_confirmation(self):
        user = self.create_user(password=None)
        response = self.client.post(
            "/api/accounts/first-access/",
            {
                "email": STUDENT_EMAIL,
                "password": NEW_PASSWORD,
                "password_confirmation": "different-password",
            },
        )

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())

    def test_first_access_accepts_explicitly_authorized_non_gmail_admin(self):
        email = " Admin.Desk@School.Example "
        stored_email = "Admin.Desk@School.Example"
        user = self.create_user(
            email=stored_email,
            role=UserProfile.Role.ADMIN,
        )
        User.objects.filter(pk=user.pk).update(email=stored_email)
        user.refresh_from_db()
        user.set_unusable_password()
        user.save(update_fields=["password"])
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=["admin.desk@school.example"],
            ADMIN_EMAILS=[" ADMIN.DESK@SCHOOL.EXAMPLE "],
        ):
            response = self.client.post(
                "/api/accounts/first-access/",
                {
                    "email": email,
                    "password": NEW_PASSWORD,
                    "password_confirmation": NEW_PASSWORD,
                },
            )

        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        self.assertEqual(user.email, stored_email)
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(user.profile.role, UserProfile.Role.ADMIN)
        audit = AccountAdminAudit.objects.get(
            action=AccountAdminAudit.Action.FIRST_ACCESS_COMPLETED,
            target=user,
        )
        self.assertNotIn(stored_email, audit.reason)
        self.assertNotIn(stored_email, str(audit.details))

        login_client = Client()
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=["admin.desk@school.example"],
            ADMIN_EMAILS=["admin.desk@school.example"],
        ):
            login_response = login_client.post(
                "/api/accounts/login/",
                {
                    "email": " ADMIN.DESK@SCHOOL.EXAMPLE ",
                    "password": NEW_PASSWORD,
                },
            )
            session = login_client.get("/api/accounts/session/").json()
        self.assertEqual(login_response.status_code, 200, login_response.content)
        self.assertTrue(session["authenticated"])

    def test_first_access_accepts_authorized_student_and_rejects_unlisted_email(self):
        student = self.create_user(
            email="student@school.example",
        )
        student.set_unusable_password()
        student.save(update_fields=["password"])
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=["student@school.example"],
            ADMIN_EMAILS=[],
        ):
            accepted = self.client.post(
                "/api/accounts/first-access/",
                {
                    "email": " STUDENT@SCHOOL.EXAMPLE ",
                    "password": NEW_PASSWORD,
                    "password_confirmation": NEW_PASSWORD,
                },
            )
            rejected = self.client.post(
                "/api/accounts/first-access/",
                {
                    "email": "unlisted@school.example",
                    "password": NEW_PASSWORD,
                    "password_confirmation": NEW_PASSWORD,
                },
            )

        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(rejected.status_code, 400)
        student.refresh_from_db()
        self.assertTrue(student.check_password(NEW_PASSWORD))
        self.assertEqual(
            AccountAdminAudit.objects.filter(
                action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                target__isnull=True,
            ).count(),
            1,
        )

    def test_first_access_rejects_authorized_admin_missing_admin_allowlist(self):
        email = "admin@school.example"
        user = self.create_user(
            email=email,
            role=UserProfile.Role.ADMIN,
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[email],
            ADMIN_EMAILS=[],
        ):
            response = self.client.post(
                "/api/accounts/first-access/",
                {
                    "email": email,
                    "password": NEW_PASSWORD,
                    "password_confirmation": NEW_PASSWORD,
                },
            )

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())
        audit = AccountAdminAudit.objects.get(
            action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
            target=user,
        )
        self.assertEqual(audit.reason, "E-mail não autorizado para papel ADMIN.")

    def test_first_access_rejects_authorized_accounts_that_are_inactive_or_unprofiled(self):
        inactive = self.create_user(
            email="inactive@school.example",
            is_active=False,
        )
        inactive.set_unusable_password()
        inactive.save(update_fields=["password"])
        unprofiled = User.objects.create_user(
            username="unprofiled",
            email="unprofiled@school.example",
        )
        unprofiled.set_unusable_password()
        unprofiled.save(update_fields=["password"])
        with self.settings(
            AUTHORIZED_LOGIN_EMAILS=[
                inactive.email,
                unprofiled.email,
            ],
            ADMIN_EMAILS=[],
        ):
            responses = [
                self.client.post(
                    "/api/accounts/first-access/",
                    {
                        "email": email,
                        "password": NEW_PASSWORD,
                        "password_confirmation": NEW_PASSWORD,
                    },
                )
                for email in (inactive.email, unprofiled.email)
            ]

        self.assertEqual([response.status_code for response in responses], [400, 400])
        inactive.refresh_from_db()
        unprofiled.refresh_from_db()
        self.assertFalse(inactive.has_usable_password())
        self.assertFalse(unprofiled.has_usable_password())
        self.assertEqual(
            AccountAdminAudit.objects.filter(
                action=AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                target__in=[inactive, unprofiled],
            ).count(),
            2,
        )

    def test_password_change_only_changes_the_authenticated_accounts_password(self):
        user = self.create_user()
        other_user = self.create_user(
            email=ADMIN_EMAIL,
            role=UserProfile.Role.ADMIN,
        )
        other_hash = other_user.password
        self.client.force_login(user)
        other_client = Client()
        other_client.force_login(user)

        response = self.client.post(
            "/api/accounts/password/change/",
            {
                "current_password": INITIAL_PASSWORD,
                "new_password": NEW_PASSWORD,
                "password_confirmation": NEW_PASSWORD,
                "user_id": other_user.pk,
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        other_user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(other_user.password, other_hash)
        self.assertTrue(self.client.get("/api/accounts/session/").json()["authenticated"])
        self.assertFalse(other_client.get("/api/accounts/session/").json()["authenticated"])
        self.assertTrue(
            AccountAdminAudit.objects.filter(
                action=AccountAdminAudit.Action.PASSWORD_CHANGED,
                actor=user,
                target=user,
            ).exists()
        )

    def test_password_change_requires_current_password_and_matching_confirmation(self):
        user = self.create_user()
        self.client.force_login(user)
        responses = (
            self.client.post(
                "/api/accounts/password/change/",
                {
                    "current_password": "wrong-password",
                    "new_password": NEW_PASSWORD,
                    "password_confirmation": NEW_PASSWORD,
                },
            ),
            self.client.post(
                "/api/accounts/password/change/",
                {
                    "current_password": INITIAL_PASSWORD,
                    "new_password": NEW_PASSWORD,
                    "password_confirmation": "different-password",
                },
            ),
        )
        self.assertEqual([response.status_code for response in responses], [400, 400])
        user.refresh_from_db()
        self.assertTrue(user.check_password(INITIAL_PASSWORD))

    def test_change_password_is_csrf_protected(self):
        user = self.create_user()
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)

        response = client.post(
            "/api/accounts/password/change/",
            {
                "current_password": INITIAL_PASSWORD,
                "new_password": NEW_PASSWORD,
                "password_confirmation": NEW_PASSWORD,
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_csrf_cookie_endpoint_and_logout(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_response = csrf_client.get("/api/accounts/csrf/")
        self.assertEqual(csrf_response.status_code, 200)
        self.assertIn("csrftoken", csrf_response.cookies)

        user = self.create_user()
        self.client.force_login(user)
        logout_response = self.client.post("/api/accounts/logout/")
        self.assertEqual(logout_response.status_code, 200)
        self.assertFalse(self.client.get("/api/accounts/session/").json()["authenticated"])

    def test_allowlist_removal_revokes_active_admin_session(self):
        admin = self.create_user(
            email=ADMIN_EMAIL,
            role=UserProfile.Role.ADMIN,
        )
        self.client.force_login(admin)
        with self.settings(ADMIN_EMAILS=[], AUTHORIZED_LOGIN_EMAILS=[]):
            session = self.client.get("/api/accounts/session/").json()

        self.assertFalse(session["authenticated"])

    def test_authentication_rate_limit_remains_active(self):
        for _ in range(9):
            self.client.post(
                "/api/accounts/login/",
                {"email": STUDENT_EMAIL, "password": "wrong-password"},
            )
        self.assertTrue(AuthRateLimitBucket.objects.exists())
