from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, override_settings

from election.models import PreferenceResponse

from .management.commands.recover_admin_password import (
    AUDIT_METHOD,
    CONFIRMATION_PHRASE,
)
from .models import AccountAdminAudit, UserProfile

User = get_user_model()
ADMIN_EMAIL = "recover.admin@gmail.com"
STUDENT_EMAIL = "recover.student@gmail.com"
STUDENT_ADMIN_EMAIL = "recover.student-admin@gmail.com"
OLD_PASSWORD = "Old-admin-password-84!"
NEW_PASSWORD = "New-local-recovery-password-93!"


class InteractiveInput(StringIO):
    def isatty(self):
        return True


@override_settings(
    ADMIN_EMAILS=[ADMIN_EMAIL],
    AUTHORIZED_LOGIN_EMAILS=[
        ADMIN_EMAIL,
        STUDENT_EMAIL,
        STUDENT_ADMIN_EMAIL,
    ],
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class RecoverAdminPasswordCommandTests(TestCase):
    def create_user(self, email, role, password=OLD_PASSWORD):
        user = User.objects.create_user(
            username=email.split("@", 1)[0],
            email=email,
            password=password,
            first_name="Preserved",
        )
        UserProfile.objects.create(
            user=user,
            role=role,
            full_name="Preserved account name",
        )
        return user

    def run_recovery(self, email, reason="Verified by the school office."):
        output = StringIO()
        with (
            patch("sys.stdin", new=InteractiveInput()),
            patch(
                "builtins.input",
                side_effect=[
                    CONFIRMATION_PHRASE,
                    reason,
                ],
            ),
            patch(
                "accounts.management.commands.recover_admin_password.getpass",
                side_effect=[email, NEW_PASSWORD, NEW_PASSWORD],
            ),
        ):
            call_command("recover_admin_password", stdout=output)
        return output.getvalue()

    def test_admin_password_is_reset_and_audited_without_changing_account_data(self):
        admin = self.create_user(ADMIN_EMAIL, UserProfile.Role.ADMIN)
        student = self.create_user(STUDENT_EMAIL, UserProfile.Role.STUDENT)
        preference = PreferenceResponse.objects.create(
            student=student,
            base_color=PreferenceResponse.BaseColor.BLACK,
            related_to_course=True,
            wants_animal=False,
            preferred_animal="",
            preferred_colors=["BLUE"],
            wants_collar=True,
            wants_phrase=False,
            intends_to_buy=True,
        )
        old_session_client = Client()
        old_session_client.force_login(admin)
        initial_audit = AccountAdminAudit.objects.create(
            actor=admin,
            actor_identifier=admin.pk,
            target=admin,
            target_identifier=admin.pk,
            action=AccountAdminAudit.Action.ACCOUNT_CREATED,
            reason="Original audit record.",
        )
        original_profile = {
            "role": admin.profile.role,
            "full_name": admin.profile.full_name,
            "credential_revision": admin.profile.credential_revision,
        }
        original_account = {
            "email": admin.email,
            "username": admin.username,
            "first_name": admin.first_name,
            "is_active": admin.is_active,
            "is_staff": admin.is_staff,
            "is_superuser": admin.is_superuser,
        }

        output = self.run_recovery(ADMIN_EMAIL)

        admin.refresh_from_db()
        self.assertTrue(admin.check_password(NEW_PASSWORD))
        self.assertFalse(admin.check_password(OLD_PASSWORD))
        self.assertEqual(admin.profile.role, UserProfile.Role.ADMIN)
        self.assertEqual(
            {
                "role": admin.profile.role,
                "full_name": admin.profile.full_name,
                "credential_revision": admin.profile.credential_revision,
            },
            original_profile,
        )
        self.assertEqual(
            {
                "email": admin.email,
                "username": admin.username,
                "first_name": admin.first_name,
                "is_active": admin.is_active,
                "is_staff": admin.is_staff,
                "is_superuser": admin.is_superuser,
            },
            original_account,
        )
        self.assertTrue(
            AccountAdminAudit.objects.filter(pk=initial_audit.pk).exists()
        )
        self.assertEqual(AccountAdminAudit.objects.count(), 2)
        event = AccountAdminAudit.objects.exclude(pk=initial_audit.pk).get()
        self.assertIsNone(event.actor)
        self.assertEqual(event.actor_identifier, 0)
        self.assertEqual(event.target, admin)
        self.assertEqual(event.action, AccountAdminAudit.Action.PASSWORD_CHANGED)
        self.assertEqual(event.reason, "Verified by the school office.")
        self.assertEqual(event.details["method"], AUDIT_METHOD)
        self.assertEqual(event.details["result"], "success")
        self.assertEqual(
            event.details["actor"],
            "local_operator_without_admin_session",
        )
        self.assertNotIn(ADMIN_EMAIL, output)
        self.assertNotIn(NEW_PASSWORD, output)
        self.assertNotIn(OLD_PASSWORD, output)

        self.assertFalse(
            old_session_client.get("/api/accounts/session/").json()[
                "authenticated"
            ]
        )
        new_client = Client()
        login = new_client.post(
            "/api/accounts/login/",
            {"email": ADMIN_EMAIL, "password": NEW_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.content)
        self.assertTrue(
            new_client.get("/api/accounts/session/").json()["authenticated"]
        )
        self.assertEqual(
            new_client.get("/api/accounts/session/").json()["user"]["role"],
            "admin",
        )
        preference.refresh_from_db()
        self.assertEqual(preference.student_id, student.pk)
        self.assertEqual(
            preference.base_color,
            PreferenceResponse.BaseColor.BLACK,
        )
        self.assertEqual(preference.preferred_colors, ["BLUE"])
        self.assertEqual(
            PreferenceResponse.objects.filter(pk=preference.pk).count(),
            1,
        )

    def test_students_and_student_admins_cannot_use_local_recovery(self):
        student = self.create_user(STUDENT_EMAIL, UserProfile.Role.STUDENT)
        student_admin = self.create_user(
            STUDENT_ADMIN_EMAIL,
            UserProfile.Role.STUDENT_ADMIN,
        )
        audit_count = AccountAdminAudit.objects.count()

        with self.settings(
            ADMIN_EMAILS=[
                ADMIN_EMAIL,
                STUDENT_EMAIL,
                STUDENT_ADMIN_EMAIL,
            ],
        ):
            for user in (student, student_admin):
                with self.subTest(role=user.profile.role):
                    with self.assertRaises(CommandError):
                        self.run_recovery(user.email)
                    user.refresh_from_db()
                    self.assertTrue(user.check_password(OLD_PASSWORD))
                    self.assertFalse(user.check_password(NEW_PASSWORD))
                    self.assertEqual(
                        user.profile.role,
                        (
                            UserProfile.Role.STUDENT
                            if user.email == STUDENT_EMAIL
                            else UserProfile.Role.STUDENT_ADMIN
                        ),
                    )

        self.assertEqual(AccountAdminAudit.objects.count(), audit_count)

    def test_cancelled_confirmation_does_not_change_password_or_audit(self):
        admin = self.create_user(ADMIN_EMAIL, UserProfile.Role.ADMIN)
        output = StringIO()
        with (
            patch("sys.stdin", new=InteractiveInput()),
            patch("builtins.input", return_value="no"),
            patch(
                "accounts.management.commands.recover_admin_password.getpass"
            ) as get_password,
        ):
            get_password.side_effect = [ADMIN_EMAIL]
            with self.assertRaises(CommandError):
                call_command("recover_admin_password", stdout=output)

        admin.refresh_from_db()
        self.assertTrue(admin.check_password(OLD_PASSWORD))
        self.assertEqual(AccountAdminAudit.objects.count(), 0)
        get_password.assert_called_once_with("Gmail da conta ADMIN autorizada: ")

    def test_command_requires_an_interactive_terminal(self):
        admin = self.create_user(ADMIN_EMAIL, UserProfile.Role.ADMIN)
        with (
            patch("sys.stdin", new=StringIO()),
            patch(
                "accounts.management.commands.recover_admin_password.getpass"
            ) as prompt,
        ):
            with self.assertRaises(CommandError):
                call_command("recover_admin_password", stdout=StringIO())

        admin.refresh_from_db()
        self.assertTrue(admin.check_password(OLD_PASSWORD))
        self.assertEqual(AccountAdminAudit.objects.count(), 0)
        prompt.assert_not_called()
