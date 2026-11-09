
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q

from .storage import PrivateProposalStorage
from .validators import validate_proposal_image


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


class ParticipationIdentity(models.Model):
    """Separates an account login from one historical participation period."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="participation_identities",
        null=True,
        blank=True,
    )
    original_user_id = models.PositiveBigIntegerField(db_index=True)
    generation = models.PositiveIntegerField()
    display_label = models.CharField(max_length=80, default="Participante")
    is_current = models.BooleanField(default=True)
    is_eligible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "generation"],
                name="election_participation_user_generation_unique",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_current=True, user__isnull=False),
                name="election_one_current_participation_identity",
            ),
        ]
        ordering = ["user_id", "generation"]

    def __str__(self):
        return self.display_label


def get_or_create_current_identity(user):
    identity = ParticipationIdentity.objects.filter(
        user_id=user.pk,
        is_current=True,
    ).first()
    if identity is not None:
        return identity
    generation = (
        ParticipationIdentity.objects.filter(user_id=user.pk)
        .aggregate(value=models.Max("generation"))["value"]
        or 0
    ) + 1
    from accounts.models import UserProfile

    role = UserProfile.objects.filter(user_id=user.pk).values_list(
        "role",
        flat=True,
    ).first()
    return ParticipationIdentity.objects.create(
        user_id=user.pk,
        original_user_id=user.pk,
        generation=generation,
        display_label=f"Participante {user.pk}-{generation}",
        is_eligible=role in ("STUDENT", "STUDENT_ADMIN"),
    )


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
        on_delete=models.SET_NULL,
        related_name="legacy_preference_responses",
        null=True,
        blank=True,
    )
    participation_identity = models.OneToOneField(
        ParticipationIdentity,
        on_delete=models.PROTECT,
        related_name="preference_response",
        null=True,
        blank=True,
    )
    is_invalidated = models.BooleanField(default=False, db_index=True)

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
        return f"Preferências de {self.participation_identity}"

    def save(self, *args, **kwargs):
        if self.student_id:
            identity = get_or_create_current_identity(self.student)
            if self.participation_identity_id != identity.pk:
                self.participation_identity = identity
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = (
                        set(kwargs["update_fields"])
                        | {"participation_identity"}
                    )
        return super().save(*args, **kwargs)


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
        on_delete=models.SET_NULL,
        related_name="legacy_shirt_proposals",
        null=True,
        blank=True,
    )
    author_identity = models.ForeignKey(
        ParticipationIdentity,
        on_delete=models.PROTECT,
        related_name="shirt_proposals",
        null=True,
        blank=True,
    )

    title = models.CharField(max_length=100)
    description = models.TextField(max_length=1000)
    image = models.ImageField(
        upload_to="shirt_proposals/",
        storage=PrivateProposalStorage(),
        validators=[validate_proposal_image],
    )

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
    is_invalidated = models.BooleanField(default=False, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_shirt_proposals",
        null=True,
        blank=True,
    )
    rejection_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()

        if not isinstance(self.title, str) or not self.title.strip():
            raise ValidationError({
                "title": "O título não pode ficar vazio."
            })

        if (
            not isinstance(self.description, str)
            or not self.description.strip()
        ):
            raise ValidationError({
                "description": "A descrição não pode ficar vazia."
            })

        if len(self.description) > 1000:
            raise ValidationError({
                "description": (
                    "A descrição deve ter no máximo 1000 caracteres."
                )
            })

        if self.has_phrase and (
            not isinstance(self.phrase_text, str)
            or not self.phrase_text.strip()
        ):
            raise ValidationError({
                "phrase_text": (
                    "Informe a frase quando a proposta tiver uma."
                )
            })

        if (
            not self.has_phrase
            and isinstance(self.phrase_text, str)
            and self.phrase_text.strip()
        ):
            raise ValidationError({
                "phrase_text": (
                    "Deixe a frase vazia quando a proposta não tiver frase."
                )
            })

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.author_id:
            identity = get_or_create_current_identity(self.author)
            if self.author_identity_id != identity.pk:
                self.author_identity = identity
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = (
                        set(kwargs["update_fields"]) | {"author_identity"}
                    )
        return super().save(*args, **kwargs)



class Vote(models.Model):
    """Voto único de um estudante em uma proposta aprovada."""

    student = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="legacy_shirt_votes",
        null=True,
        blank=True,
    )
    participation_identity = models.ForeignKey(
        ParticipationIdentity,
        on_delete=models.PROTECT,
        related_name="votes",
        null=True,
        blank=True,
    )

    proposal = models.ForeignKey(
        ShirtProposal,
        on_delete=models.PROTECT,
        related_name="votes",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    is_invalidated = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["participation_identity"],
                condition=Q(is_invalidated=False),
                name="election_one_valid_vote_per_identity",
            ),
        ]

    def clean(self):
        super().clean()

        if self.proposal_id and self.proposal.status != (
            ShirtProposal.Status.APPROVED
        ):
            raise ValidationError({
                "proposal": "Só é possível votar em propostas aprovadas."
            })

    def __str__(self):
        return f"{self.participation_identity} → {self.proposal}"

    def save(self, *args, **kwargs):
        if self.student_id:
            identity = get_or_create_current_identity(self.student)
            if self.participation_identity_id != identity.pk:
                self.participation_identity = identity
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = (
                        set(kwargs["update_fields"])
                        | {"participation_identity"}
                    )
        return super().save(*args, **kwargs)


class ParticipationAuditEvent(models.Model):
    class ParticipationType(models.TextChoices):
        PREFERENCE_RESPONSE = "PREFERENCE_RESPONSE", "Resposta da pesquisa"
        VOTE = "VOTE", "Voto"
        PROPOSAL = "PROPOSAL", "Proposta"

    class Action(models.TextChoices):
        INVALIDATE = "INVALIDATE", "Invalidada"
        RESTORE = "RESTORE", "Restaurada"
        AUTHORIZE_REPLACEMENT_VOTE = (
            "AUTHORIZE_REPLACEMENT_VOTE",
            "Novo voto autorizado",
        )

    participation_type = models.CharField(
        max_length=20,
        choices=ParticipationType.choices,
    )
    participation_id = models.PositiveBigIntegerField()
    participation_identity = models.ForeignKey(
        ParticipationIdentity,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="participation_audit_events",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField()
    action = models.CharField(max_length=32, choices=Action.choices)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class VoteReplacementAuthorization(models.Model):
    participation_identity = models.ForeignKey(
        ParticipationIdentity,
        on_delete=models.PROTECT,
        related_name="vote_replacement_authorizations",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="vote_replacement_authorizations",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField()
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["participation_identity"],
                condition=Q(consumed_at__isnull=True),
                name="election_one_pending_vote_replacement",
            ),
        ]


class ImmutableResultSnapshotQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Snapshots de resultados são imutáveis.")

    def delete(self):
        raise ValidationError("Snapshots de resultados não podem ser excluídos.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Snapshots de resultados são imutáveis.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Crie snapshots individualmente para validar sua integridade.")


class ElectionResultSnapshotManager(
    models.Manager.from_queryset(ImmutableResultSnapshotQuerySet)
):
    use_in_migrations = False


class ElectionResultSnapshot(models.Model):
    sequence = models.PositiveIntegerField(unique=True)
    payload = models.JSONField(default=dict)
    image_manifest = models.JSONField(default=dict)
    payload_sha256 = models.CharField(max_length=64)
    state_sha256 = models.CharField(max_length=64)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="result_snapshots",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField(null=True, blank=True)
    trigger = models.CharField(max_length=40)
    previous = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="subsequent_snapshots",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sequence"]

    objects = ElectionResultSnapshotManager()

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Snapshots de resultados são imutáveis.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Snapshots de resultados não podem ser excluídos.")


class ElectionSettings(models.Model):
    """Configurações gerais da pesquisa e da votação."""

    survey_open = models.BooleanField(default=False)
    proposal_submission_open = models.BooleanField(default=True)
    voting_open = models.BooleanField(default=False)
    # Controls authorization to disclose calculated results, not calculation.
    results_release_authorized = models.BooleanField(
        "autorizar divulgação dos resultados",
        default=False,
    )

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

        if self.voting_open and self.proposal_submission_open is not False:
            raise ValidationError(
                "Feche o envio de propostas antes de abrir a votação."
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


class MaintenanceConfirmation(models.Model):
    class Action(models.TextChoices):
        RESET_USER_BASIC = "RESET_USER_BASIC", "Reinicializar conta"
        RESET_USER_FULL = "RESET_USER_FULL", "Reinicializar conta integralmente"
        DELETE_USER = "DELETE_USER", "Excluir conta"
        DELETE_PROPOSAL = "DELETE_PROPOSAL", "Excluir proposta"
        RESET_ELECTION = "RESET_ELECTION", "Reiniciar eleição"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="maintenance_confirmations",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField(default=0)
    token_digest = models.CharField(max_length=64, unique=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    summary = models.JSONField(default=dict)
    snapshot_digest = models.CharField(max_length=64)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()


class MaintenanceAudit(models.Model):
    class Action(models.TextChoices):
        RESET_USER_BASIC = "RESET_USER_BASIC", "Reinicializar conta"
        RESET_USER_FULL = "RESET_USER_FULL", "Reinicializar conta integralmente"
        DELETE_USER = "DELETE_USER", "Excluir conta"
        DELETE_PROPOSAL = "DELETE_PROPOSAL", "Excluir proposta"
        RESET_ELECTION = "RESET_ELECTION", "Reiniciar eleição"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="maintenance_audit_events",
        null=True,
        blank=True,
    )
    actor_identifier = models.PositiveBigIntegerField()
    target_identifier = models.PositiveBigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    affected_counts = models.JSONField(default=dict)
    reason = models.TextField(blank=True, default="")
    result = models.CharField(max_length=20, default="completed")
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class MaintenanceFileCleanup(models.Model):
    storage_name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
