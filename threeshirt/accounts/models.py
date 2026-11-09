
import uuid

from django.conf import settings
from django.db import models

class UserProfile(models.Model):
    """Armazena o perfil e o papel de cada usuário."""

    class Role(models.TextChoices):
        STUDENT = "STUDENT", "Estudante"
        STUDENT_ADMIN = "STUDENT_ADMIN", "Estudante-administrador"
        ADMIN = "ADMIN", "Administrador"

    # Papéis que podem participar da eleição.
    STUDENT_ROLES = (
        Role.STUDENT,
        Role.STUDENT_ADMIN,
    )

    # Papéis que podem acessar a administração.
    ADMIN_ROLES = (
        Role.ADMIN,
        Role.STUDENT_ADMIN,
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    full_name = models.CharField(
        "nome completo",
        max_length=150,
        blank=True,
        default="",
    )

    role = models.CharField(
        max_length=13,
        choices=Role.choices,
        default=Role.STUDENT,
    )

    credential_revision = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return self.full_name or self.user.email


class PasswordAccessRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pendente"
        APPROVED = "APPROVED", "Aprovada"
        REJECTED = "REJECTED", "Rejeitada"
        COMPLETED = "COMPLETED", "Concluída"
        EXPIRED = "EXPIRED", "Expirada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="password_access_requests",
        null=True,
        blank=True,
    )
    email_digest = models.CharField(max_length=64)
    session_key_digest = models.CharField(max_length=64)
    credential_revision = models.PositiveBigIntegerField(default=0)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_password_access_requests",
        null=True,
        blank=True,
    )
    review_reason = models.TextField(blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    closed_reason = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
    def __str__(self):
        return f"Solicitação de senha {self.pk}"


class AuthRateLimitBucket(models.Model):
    key_digest = models.CharField(max_length=64, unique=True)
    request_timestamps = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def __str__(self):
        return "Limite de autenticação"


class FirstAccessCredential(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="first_access_credentials",
        null=True,
        blank=True,
    )
    target_identifier = models.PositiveBigIntegerField()
    token_digest = models.CharField(max_length=64, unique=True)
    credential_revision = models.PositiveBigIntegerField(default=0)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="issued_first_access_credentials",
        null=True,
        blank=True,
    )
    issuer_identifier = models.PositiveBigIntegerField(null=True, blank=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["user", "consumed_at", "revoked_at"],
                name="acct_first_access_active_idx",
            ),
        ]


class AccountAdminAudit(models.Model):
    class Action(models.TextChoices):
        PASSWORD_REQUEST_APPROVED = "PASSWORD_REQUEST_APPROVED", "Solicitação aprovada"
        PASSWORD_REQUEST_REJECTED = "PASSWORD_REQUEST_REJECTED", "Solicitação rejeitada"
        FIRST_ACCESS_COMPLETED = "FIRST_ACCESS_COMPLETED", "Primeiro acesso concluído"
        FIRST_ACCESS_REJECTED = "FIRST_ACCESS_REJECTED", "Tentativa de primeiro acesso rejeitada"
        PASSWORD_CHANGED = "PASSWORD_CHANGED", "Senha alterada"
        ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED", "Conta bloqueada"
        ACCOUNT_REACTIVATED = "ACCOUNT_REACTIVATED", "Conta reativada"
        ACCOUNT_RESET = "ACCOUNT_RESET", "Conta reinicializada"
        ROLE_CHANGED = "ROLE_CHANGED", "Papel alterado"
        ACCOUNT_CREATED = "ACCOUNT_CREATED", "Conta criada"
        FIRST_ACCESS_ISSUED = (
            "FIRST_ACCESS_ISSUED",
            "Credencial de primeiro acesso emitida",
        )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="account_admin_audit_events",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField()
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="account_target_audit_events",
        null=True,
        blank=True,
    )
    target_identifier = models.PositiveBigIntegerField()
    action = models.CharField(max_length=32, choices=Action.choices)
    reason = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
