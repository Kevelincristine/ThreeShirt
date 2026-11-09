import sys
from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.access import is_authorized_admin_email, normalize_gmail_email
from accounts.account_sessions import revoke_user_sessions
from accounts.models import AccountAdminAudit, UserProfile

User = get_user_model()
CONFIRMATION_PHRASE = "RECUPERAR SENHA ADMINISTRATIVA"
AUDIT_METHOD = "local_admin_password_recovery"


class Command(BaseCommand):
    help = "Recupera interativamente a senha de uma conta ADMIN autorizada."

    def handle(self, *args, **options):
        if not sys.stdin.isatty():
            raise CommandError(
                "Execute a recuperação em um terminal local interativo."
            )

        email = normalize_gmail_email(
            getpass("Gmail da conta ADMIN autorizada: ")
        )
        if not email or not is_authorized_admin_email(email):
            raise CommandError("A conta não está autorizada para recuperação.")

        matches = list(
            User.objects.filter(email__iexact=email).order_by("pk")[:2]
        )
        if len(matches) != 1:
            raise CommandError(
                "A recuperação exige uma única conta existente e autorizada."
            )
        user = matches[0]
        try:
            profile = user.profile
        except UserProfile.DoesNotExist as exc:
            raise CommandError(
                "A conta não possui um perfil ADMIN válido."
            ) from exc
        if not user.is_active or profile.role != UserProfile.Role.ADMIN:
            raise CommandError(
                "A recuperação só pode ser usada por uma conta ADMIN ativa."
            )

        self.stdout.write("Conta ADMIN elegível encontrada.")
        if input(
            f"Digite {CONFIRMATION_PHRASE} para continuar: "
        ).strip() != CONFIRMATION_PHRASE:
            raise CommandError("Operação cancelada.")

        reason = input("Justificativa da recuperação: ").strip()
        if not reason:
            raise CommandError("A justificativa é obrigatória.")

        password = getpass("Nova senha: ")
        confirmation = getpass("Confirme a nova senha: ")
        if not password or len(password) > 1024 or password != confirmation:
            raise CommandError("As senhas não coincidem ou são inválidas.")
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(
                "A nova senha não atende aos requisitos de segurança."
            ) from exc

        original_password_hash = user.password
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=user.pk)
            try:
                profile = UserProfile.objects.select_for_update().get(
                    user_id=user.pk
                )
            except UserProfile.DoesNotExist as exc:
                raise CommandError(
                    "O perfil da conta mudou durante a operação."
                ) from exc
            current_email = normalize_gmail_email(user.email)
            if (
                user.password != original_password_hash
                or not user.is_active
                or current_email != email
                or not is_authorized_admin_email(email)
                or profile.role != UserProfile.Role.ADMIN
                or User.objects.filter(email__iexact=email)
                .exclude(pk=user.pk)
                .exists()
            ):
                raise CommandError(
                    "A conta mudou ou deixou de ser elegível para recuperação."
                )

            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(
                    "A nova senha não atende aos requisitos de segurança."
                ) from exc

            user.set_password(password)
            user.save(update_fields=["password"])
            revoke_user_sessions(user.pk)
            AccountAdminAudit.objects.create(
                actor=None,
                actor_identifier=0,
                target=user,
                target_identifier=user.pk,
                action=AccountAdminAudit.Action.PASSWORD_CHANGED,
                reason=reason,
                details={
                    "method": AUDIT_METHOD,
                    "result": "success",
                    "actor": "local_operator_without_admin_session",
                },
            )

        self.stdout.write(
            self.style.SUCCESS("Senha administrativa recuperada com sucesso.")
        )
