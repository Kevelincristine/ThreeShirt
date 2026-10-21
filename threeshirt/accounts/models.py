
from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    """Armazena o perfil e o papel de cada usuário."""

    class Role(models.TextChoices):
        STUDENT = "STUDENT", "Estudante"
        ADMIN = "ADMIN", "Administrador"

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
        max_length=10,
        choices=Role.choices,
        default=Role.STUDENT,
    )

    def __str__(self):
        return self.full_name or self.user.email


class EmailLoginCode(models.Model):
    """Armazena os dados necessários para validar um código de acesso."""

    email = models.EmailField(db_index=True)
    code_hash = models.CharField(max_length=128)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f"Código de acesso para {self.email}"


class LoginRequestRateBucket(models.Model):
    """Stores a rolling request window for a pseudonymous rate-limit key."""

    key_digest = models.CharField(max_length=64, unique=True)
    request_timestamps = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def __str__(self):
        return "Limite de solicitações de login"
