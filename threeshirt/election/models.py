
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q


COLORS = [
    ("RED", "Vermelho"),
    ("BLUE", "Azul"),
    ("YELLOW", "Amarelo"),
    ("GREEN", "Verde"),
    ("ORANGE", "Laranja"),
    ("PURPLE", "Roxo/Violeta"),
    ("PINK", "Rosa"),
    ("BROWN", "Marrom"),
    ("BLACK", "Preto"),
    ("WHITE", "Branco"),
]

COLOR_CODES = {code for code, label in COLORS}


def validate_colors(value):
    """Aceita uma lista de cores válidas, sem repetições."""
    if not isinstance(value, list):
        raise ValidationError("As cores devem ser enviadas como uma lista.")

    if len(value) != len(set(value)):
        raise ValidationError("Não é permitido repetir cores.")

    invalid = set(value) - COLOR_CODES
    if invalid:
        raise ValidationError(
            f"Cores inválidas: {', '.join(sorted(invalid))}."
        )


class PreferenceResponse(models.Model):
    """Respostas da pesquisa preenchida por um estudante."""

    class BaseColor(models.TextChoices):
        BLACK = "BLACK", "Preta"
        WHITE = "WHITE", "Branca"

    student = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preference_response",
    )

    base_color = models.CharField(
        "cor-base preferida",
        max_length=5,
        choices=BaseColor.choices,
    )

    related_to_course = models.BooleanField(
        "relacionada ao curso técnico"
    )

    wants_animal = models.BooleanField("deseja tema com animal")

    preferred_animal = models.CharField(
        "animal preferido",
        max_length=60,
        blank=True,
    )

    preferred_colors = models.JSONField(
        "cores preferidas",
        default=list,
        validators=[validate_colors],
    )

    wants_collar = models.BooleanField("prefere camiseta com gola")

    wants_phrase = models.BooleanField("deseja uma frase")

    intends_to_buy = models.BooleanField(
        "pretende comprar a camiseta"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()

        if self.wants_animal and not self.preferred_animal.strip():
            raise ValidationError({
                "preferred_animal": (
                    "Informe o animal preferido quando a resposta for sim."
                )
            })

        if not self.wants_animal and self.preferred_animal.strip():
            raise ValidationError({
                "preferred_animal": (
                    "O animal deve ficar vazio quando a resposta for não."
                )
            })

    def __str__(self):
        return f"Preferências de {self.student}"


class ShirtProposal(models.Model):
    """Uma proposta de camiseta enviada por um estudante."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pendente"
        APPROVED = "APPROVED", "Aprovada"
        REJECTED = "REJECTED", "Rejeitada"

    class BaseColor(models.TextChoices):
        BLACK = "BLACK", "Preta"
        WHITE = "WHITE", "Branca"

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shirt_proposals",
    )

    title = models.CharField(max_length=100)
    description = models.TextField(max_length=1000)
    image = models.ImageField(upload_to="shirt_proposals/")

    base_color = models.CharField(
        max_length=5,
        choices=BaseColor.choices,
    )

    related_to_course = models.BooleanField()
    animal = models.CharField(max_length=60, blank=True)

    additional_colors = models.JSONField(
        default=list,
        validators=[validate_colors],
    )

    has_collar = models.BooleanField()
    has_phrase = models.BooleanField()
    phrase_text = models.CharField(max_length=200, blank=True)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()

        if self.has_phrase and not self.phrase_text.strip():
            raise ValidationError({
                "phrase_text": (
                    "Informe a frase quando a proposta tiver uma."
                )
            })

        if not self.has_phrase and self.phrase_text.strip():
            raise ValidationError({
                "phrase_text": (
                    "Deixe a frase vazia quando a proposta não tiver frase."
                )
            })

    def __str__(self):
        return self.title



class Vote(models.Model):
    """Voto único de um estudante em uma proposta aprovada."""

    student = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shirt_vote",
    )

    proposal = models.ForeignKey(
        ShirtProposal,
        on_delete=models.PROTECT,
        related_name="votes",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()

        if self.proposal_id and self.proposal.status != (
            ShirtProposal.Status.APPROVED
        ):
            raise ValidationError({
                "proposal": "Só é possível votar em propostas aprovadas."
            })

    def __str__(self):
        return f"{self.student} → {self.proposal}"


class ElectionSettings(models.Model):
    """Configurações gerais da pesquisa e da votação."""

    survey_open = models.BooleanField(default=False)
    voting_open = models.BooleanField(default=False)

    survey_closed_at = models.DateTimeField(null=True, blank=True)
    voting_started_at = models.DateTimeField(null=True, blank=True)
    voting_ended_at = models.DateTimeField(null=True, blank=True)

    compatibility_weight = models.PositiveSmallIntegerField(
        default=50,
        validators=[
            MinValueValidator(0),
            MaxValueValidator(100),
        ],
        help_text="Peso percentual da compatibilidade nos resultados.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuração da votação"
        verbose_name_plural = "Configurações da votação"

    def clean(self):
        super().clean()

        if self.voting_open and self.survey_open:
            raise ValidationError(
                "Feche a pesquisa de preferências antes de abrir a votação."
            )

        if self.voting_open and not self.voting_started_at:
            raise ValidationError({
                "voting_started_at": (
                    "Informe quando a votação foi iniciada."
                )
            })

    def save(self, *args, **kwargs):
        # Mantém uma única configuração geral para esta turma.
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "As configurações gerais não podem ser excluídas."
        )

    def __str__(self):
        return "Configurações do ThreeShirt"
