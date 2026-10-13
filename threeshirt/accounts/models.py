
from django.conf import settings
from django.db import models

class UserProfile(models.Model):
    """Armazena o tipo de acesso de cada usuário."""

    class Role(models.TextChoices):
        STUDENT = 'STUDENT', 'Estudante'
        ADMIN = 'ADMIN', 'Administrador'

    # Cada usuário terá um único perfil.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile'
    )

    # Define as permissões gerais da conta no ThreeShirt.
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.STUDENT
    )

    def __str__(self):
        return f'{self.user.email} - {self.get_role_display()}'


class EmailLoginCode(models.Model):
    """Armazena os dados necessários para validar um código de acesso."""

    email = models.EmailField(db_index=True)

    # Guardaremos o código protegido por hash, não em texto puro.
    code_hash = models.CharField(max_length=128)

    # Controlam a validade e o uso do código.
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    # Limita tentativas de adivinhação do código.
    attempts = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return f'Código de acesso para {self.email}'
