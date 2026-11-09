"""Business logic for comparing student preferences with shirt proposals."""

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth import get_user_model
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.urls import reverse

from accounts.models import UserProfile

from .models import (
    COLORS,
    COLOR_CODES,
    ElectionSettings,
    ElectionResultSnapshot,
    PreferenceResponse,
    ParticipationIdentity,
    ShirtProposal,
    Vote,
    VoteReplacementAuthorization,
    get_or_create_current_identity,
)
from .concurrency import run_with_sqlite_lock_retry
from .validators import get_proposal_image_format


MIN_COMPARABLE_CRITERIA = 3
COMPATIBILITY_TIE_QUANTUM = Decimal("0.01")

SHIRT_PROPOSAL_FIELDS = {
    "title",
    "description",
    "image",
    "base_color",
    "related_to_course",
    "animal",
    "additional_colors",
    "has_collar",
    "has_phrase",
    "phrase_text",
}

PREFERENCE_FIELDS = {
    "base_color",
    "related_to_course",
    "wants_animal",
    "preferred_animal",
    "preferred_colors",
    "wants_collar",
    "wants_phrase",
    "intends_to_buy",
}

BOOLEAN_PREFERENCE_FIELDS = {
    "related_to_course",
    "wants_animal",
    "wants_collar",
    "wants_phrase",
    "intends_to_buy",
}


def _require_student_user(user):
    """Ensure that the authenticated user can participate as a student."""

    if (
        not getattr(user, "is_authenticated", False)
    ):
        raise PermissionDenied("É necessário um usuário autenticado.")

    account = (
        get_user_model().objects
        .select_related("profile")
        .filter(pk=user.pk, is_active=True)
        .first()
    )
    if account is None:
        raise PermissionDenied("É necessário um usuário ativo.")
    try:
        profile = account.profile
    except UserProfile.DoesNotExist as exc:
        raise PermissionDenied(
            "O usuário precisa ter um perfil de estudante."
        ) from exc

    if profile.role not in UserProfile.STUDENT_ROLES:
        raise PermissionDenied(
            "Somente estudantes podem responder à pesquisa."
        )
    return account


def _current_identity(user, lock=False):
    account = _require_student_user(user)
    identities = ParticipationIdentity.objects.filter(
        user_id=account.pk,
        is_current=True,
    )
    if lock:
        identities = identities.select_for_update()
    identity = identities.first()
    if identity is None:
        identity = get_or_create_current_identity(account)
    return account, identity


def cast_vote(user, proposal_id):
    """Register one vote for an approved proposal during an open election."""

    _require_student_user(user)

    return run_with_sqlite_lock_retry(
        lambda: _cast_vote_once(user, proposal_id)
    )


def _cast_vote_once(user, proposal_id):
    with transaction.atomic():
        settings = (
            ElectionSettings.objects
            .select_for_update()
            .filter(pk=1)
            .first()
        )

        if settings is None or not settings.voting_open:
            raise ValidationError({"voting": "A votação está fechada."})

        _account, identity = _current_identity(user, lock=True)
        proposal = (
            ShirtProposal.objects
            .select_for_update()
            .filter(pk=proposal_id)
            .first()
        )

        if proposal is None:
            raise ValidationError({
                "proposal": "A proposta selecionada não existe."
            })

        if (
            proposal.status != ShirtProposal.Status.APPROVED
            or proposal.is_invalidated
        ):
            raise ValidationError({
                "proposal": "Só é possível votar em propostas aprovadas."
            })

        if proposal.author_identity_id == identity.pk:
            raise ValidationError({
                "proposal": "Você não pode votar na sua própria proposta."
            })

        if Vote.objects.filter(
            participation_identity=identity,
            is_invalidated=False,
        ).exists():
            raise ValidationError({
                "vote": "Você já registrou seu voto."
            })

        replacement = None
        if Vote.objects.filter(participation_identity=identity).exists():
            replacement = (
                VoteReplacementAuthorization.objects.select_for_update()
                .filter(
                    participation_identity=identity,
                    consumed_at__isnull=True,
                )
                .first()
            )
            if replacement is None:
                raise ValidationError({
                    "vote": (
                        "É necessária uma autorização administrativa para "
                        "registrar um novo voto."
                    )
                })
        try:
            with transaction.atomic():
                vote = Vote.objects.create(
                    student=None,
                    participation_identity=identity,
                    proposal=proposal,
                )
        except IntegrityError as exc:
            if Vote.objects.filter(
                participation_identity=identity,
                is_invalidated=False,
            ).exists():
                raise ValidationError({
                    "vote": "Você já registrou seu voto."
                }) from exc

            raise
        if replacement is not None:
            replacement.consumed_at = vote.created_at
            replacement.save(update_fields=["consumed_at"])

    return vote


def _has_valid_colors(value):
    return (
        isinstance(value, list)
        and all(
            isinstance(color, str) and color in COLOR_CODES
            for color in value
        )
        and len(value) == len(set(value))
    )


def save_preference_response(user, data):
    """Create or update the authenticated student's open-survey response."""

    _require_student_user(user)

    if not isinstance(data, Mapping):
        raise ValidationError("As respostas devem ser enviadas como um objeto.")

    unknown_fields = set(data) - PREFERENCE_FIELDS

    if unknown_fields:
        errors = {
            str(field): "Este campo não pode ser enviado."
            for field in unknown_fields
        }
        raise ValidationError(errors)

    with transaction.atomic():
        settings = (
            ElectionSettings.objects
            .select_for_update()
            .filter(pk=1)
            .first()
        )

        if settings is None or not settings.survey_open:
            raise ValidationError(
                {"survey": "A pesquisa de preferências está fechada."}
            )

        _account, identity = _current_identity(user, lock=True)
        response = (
            PreferenceResponse.objects
            .select_for_update()
            .filter(participation_identity=identity)
            .first()
        )

        if response is None:
            response = PreferenceResponse(
                student=None,
                participation_identity=identity,
            )
        elif response.is_invalidated:
            raise ValidationError({
                "survey": (
                    "A resposta foi invalidada e não pode ser alterada."
                )
            })

        for field, value in data.items():
            setattr(response, field, value)

        invalid_types = {
            field: "Informe um valor booleano."
            for field in BOOLEAN_PREFERENCE_FIELDS
            if type(getattr(response, field)) is not bool
        }

        if not isinstance(response.preferred_animal, str):
            invalid_types["preferred_animal"] = "Informe um texto."

        if not isinstance(response.preferred_colors, list) or any(
            not isinstance(color, str)
            for color in response.preferred_colors
        ):
            invalid_types["preferred_colors"] = (
                "As cores devem ser uma lista de textos."
            )

        if invalid_types:
            raise ValidationError(invalid_types)

        response.full_clean()
        response.save()

    return response


def submit_shirt_proposal(user, data):
    """Validate and submit a proposal as pending for the current student."""

    _require_student_user(user)

    if not isinstance(data, Mapping):
        raise ValidationError("Os dados da proposta devem ser um objeto.")

    unknown_fields = set(data) - SHIRT_PROPOSAL_FIELDS

    if unknown_fields:
        raise ValidationError({
            str(field): "Este campo não pode ser enviado."
            for field in unknown_fields
        })

    with transaction.atomic():
        settings = (
            ElectionSettings.objects
            .select_for_update()
            .filter(pk=1)
            .first()
        )
        if settings is not None and settings.proposal_submission_open is False:
            raise ValidationError({
                "proposal_submission": "O envio de propostas está fechado."
            })
        _account, identity = _current_identity(user, lock=True)
        proposal = ShirtProposal(
            author=None,
            author_identity=identity,
            status=ShirtProposal.Status.PENDING,
        )

        for field, value in data.items():
            setattr(proposal, field, value)

        proposal.full_clean()
        proposal.save()

    return proposal


def get_preference_response(user):
    """Return only the authenticated student's response, if one exists."""

    _require_student_user(user)

    _account, identity = _current_identity(user)
    return PreferenceResponse.objects.filter(
        participation_identity=identity,
        is_invalidated=False,
    ).first()


def is_survey_open():
    """Return whether the singleton preference survey is open."""

    settings = ElectionSettings.objects.filter(pk=1).first()

    return settings is not None and settings.survey_open


def calculate_compatibility(
    preferences: PreferenceResponse,
    proposal: ShirtProposal,
) -> tuple[float | None, int]:
    """Return the individual compatibility percentage and comparable criteria."""

    similarities = []

    valid_base_colors = {
        value for value, _label in PreferenceResponse.BaseColor.choices
    }

    if (
        preferences.base_color in valid_base_colors
        and proposal.base_color in valid_base_colors
    ):
        similarities.append(
            float(preferences.base_color == proposal.base_color)
        )

    boolean_pairs = (
        (preferences.related_to_course, proposal.related_to_course),
        (preferences.wants_collar, proposal.has_collar),
        (preferences.wants_phrase, proposal.has_phrase),
    )

    for preference_value, proposal_value in boolean_pairs:
        if isinstance(preference_value, bool) and isinstance(
            proposal_value, bool
        ):
            similarities.append(
                float(preference_value == proposal_value)
            )

    preferred_colors = preferences.preferred_colors
    proposal_colors = proposal.additional_colors

    if (
        _has_valid_colors(preferred_colors)
        and preferred_colors
        and _has_valid_colors(proposal_colors)
    ):
        preferred_color_set = set(preferred_colors)
        proposal_color_set = set(proposal_colors)
        union = preferred_color_set | proposal_color_set

        similarities.append(
            len(preferred_color_set & proposal_color_set) / len(union)
        )

    preferred_animal = preferences.preferred_animal
    proposal_animal = proposal.animal

    if (
        preferences.wants_animal is True
        and isinstance(preferred_animal, str)
        and preferred_animal.strip()
        and isinstance(proposal_animal, str)
        and proposal_animal.strip()
    ):
        similarities.append(
            float(
                preferred_animal.strip().casefold()
                == proposal_animal.strip().casefold()
            )
        )

    elif (
        preferences.wants_animal is False
        and isinstance(preferred_animal, str)
        and not preferred_animal.strip()
        and isinstance(proposal_animal, str)
        and proposal_animal.strip()
    ):
        similarities.append(0.0)

    comparable_criteria = len(similarities)

    if comparable_criteria < MIN_COMPARABLE_CRITERIA:
        return None, comparable_criteria

    score = round(
        sum(similarities) / comparable_criteria * 100,
        2,
    )

    return score, comparable_criteria


def get_approved_proposal_compatibilities(user):
    """Return individual scores for approved proposals and this student."""

    _require_student_user(user)

    preferences = get_preference_response(user)

    if preferences is None:
        return []

    results = []

    proposals = ShirtProposal.objects.filter(
        status=ShirtProposal.Status.APPROVED,
        is_invalidated=False,
    )

    for proposal in proposals:
        score, comparable_criteria = calculate_compatibility(
            preferences,
            proposal,
        )

        results.append((proposal, score, comparable_criteria))

    return results


def get_pending_proposal_descriptions(user):
    """Return pending proposal text without image names or file paths."""

    _require_student_user(user)

    return list(
        ShirtProposal.objects
        .filter(
            status=ShirtProposal.Status.PENDING,
            is_invalidated=False,
        )
        .values("id", "title", "description")
    )


def get_approved_proposals(user):
    """Return all approved proposals, newest first, for student voters."""

    _require_student_user(user)

    return ShirtProposal.objects.filter(
        status=ShirtProposal.Status.APPROVED,
        is_invalidated=False,
    ).order_by("-created_at", "-pk")


def get_vote_tally():
    """Count eligible votes without erasing invalidated or excluded records."""

    settings = ElectionSettings.objects.filter(pk=1).first()
    preserve_released_results = (
        settings is not None
        and not settings.voting_open
        and settings.results_release_authorized
    )
    proposal_counts = list(
        ShirtProposal.objects
        .values(
            "pk",
            "title",
            "status",
            "is_invalidated",
            "author_identity_id",
        )
        .annotate(
            total_count=Count("votes"),
            valid_count=Count(
                "votes",
                filter=Q(
                    votes__is_invalidated=False,
                    votes__participation_identity__is_eligible=True,
                ),
            ),
            self_vote_count=Count(
                "votes",
                filter=Q(
                    votes__is_invalidated=False,
                    votes__participation_identity__is_eligible=True,
                    votes__participation_identity_id=F("author_identity_id"),
                    author_identity_id__isnull=False,
                ),
            ),
            invalidated_count=Count(
                "votes",
                filter=Q(votes__is_invalidated=True),
            ),
        )
        .order_by("pk")
    )

    approved_proposals = []
    excluded_proposals = []
    for proposal in proposal_counts:
        excluded_count = 0
        eligible = (
            proposal["status"] == ShirtProposal.Status.APPROVED
            and not proposal["is_invalidated"]
        )
        if eligible:
            eligible_votes = proposal["valid_count"]
            if not preserve_released_results:
                eligible_votes -= proposal["self_vote_count"]
            excluded_count = proposal["total_count"] - eligible_votes
            approved_proposals.append({
                **proposal,
                "vote_count": eligible_votes,
            })
        else:
            eligible_votes = 0
            excluded_count = proposal["total_count"]

        if excluded_count:
            excluded_proposals.append({
                "proposal_id": proposal["pk"],
                "title": proposal["title"],
                "status": proposal["status"],
                "is_invalidated": proposal["is_invalidated"],
                "vote_count": excluded_count,
                "reason": (
                    "proposal_invalidated"
                    if proposal["is_invalidated"]
                    else "not_approved"
                    if proposal["status"] != ShirtProposal.Status.APPROVED
                    else "vote_invalidated_or_ineligible"
                ),
            })

    total_votes = sum(proposal["vote_count"] for proposal in approved_proposals)

    results = []

    for proposal in approved_proposals:
        percentage = (
            Decimal(proposal["vote_count"] * 100) / Decimal(total_votes)
            if total_votes
            else None
        )

        results.append({
            "proposal_id": proposal["pk"],
            "title": proposal["title"],
            "vote_count": proposal["vote_count"],
            "percentage": percentage,
        })

    highest_count = max(
        (result["vote_count"] for result in results),
        default=0,
    )

    winners = (
        [
            result for result in results
            if result["vote_count"] == highest_count
        ]
        if total_votes
        else []
    )

    return {
        "status": "complete" if total_votes else "no_votes",
        "total_votes": total_votes,
        "excluded_votes": sum(proposal["vote_count"] for proposal in excluded_proposals),
        "excluded_proposals": excluded_proposals,
        "proposals": results,
        "winners": winners,
        "is_tie": len(winners) > 1,
    }


def get_preference_tally():
    """Aggregate all valid historical participation generations."""

    responses = list(
        PreferenceResponse.objects.filter(
            is_invalidated=False,
            participation_identity__is_eligible=True,
        ).order_by("pk")
    )
    respondent_count = len(responses)

    def build_question(question_id, question, denominator, options):
        return {
            "id": question_id,
            "question": question,
            "respondent_count": denominator,
            "options": [
                {
                    "value": value,
                    "label": label,
                    "response_count": count,
                    "percentage": (
                        Decimal(count * 100) / Decimal(denominator)
                        if denominator
                        else None
                    ),
                }
                for value, label, count in options
            ],
        }

    boolean_options = ((True, "Sim"), (False, "Não"))

    def boolean_question_options(field):
        return [
            (
                str(value).lower(),
                label,
                sum(getattr(response, field) is value for response in responses),
            )
            for value, label in boolean_options
        ]

    base_color_options = [
        (
            value,
            label,
            sum(response.base_color == value for response in responses),
        )
        for value, label in PreferenceResponse.BaseColor.choices
    ]
    wants_animal_count = sum(response.wants_animal for response in responses)
    animal_counts = {}
    for response in responses:
        if response.wants_animal and response.preferred_animal:
            animal_counts[response.preferred_animal] = (
                animal_counts.get(response.preferred_animal, 0) + 1
            )

    questions = [
        build_question(
            "base_color",
            "Cor-base preferida",
            respondent_count,
            base_color_options,
        ),
        build_question(
            "related_to_course",
            "Você gostaria que a camiseta fosse relacionada ao curso?",
            respondent_count,
            boolean_question_options("related_to_course"),
        ),
        build_question(
            "wants_animal",
            "Você gostaria de um tema com animal?",
            respondent_count,
            boolean_question_options("wants_animal"),
        ),
        build_question(
            "preferred_animal",
            "Qual animal?",
            wants_animal_count,
            [
                (animal, animal, count)
                for animal, count in sorted(
                    animal_counts.items(),
                    key=lambda item: (item[0].casefold(), item[0]),
                )
            ],
        ),
        build_question(
            "preferred_colors",
            "Quais cores adicionais você prefere? (opcional)",
            respondent_count,
            [
                (
                    value,
                    label,
                    sum(value in response.preferred_colors for response in responses),
                )
                for value, label in COLORS
            ],
        ),
        build_question(
            "wants_collar",
            "Você prefere camiseta com gola?",
            respondent_count,
            boolean_question_options("wants_collar"),
        ),
        build_question(
            "wants_phrase",
            "Você gostaria de uma frase na camiseta?",
            respondent_count,
            boolean_question_options("wants_phrase"),
        ),
        build_question(
            "intends_to_buy",
            "Você pretende comprar a camiseta?",
            respondent_count,
            boolean_question_options("intends_to_buy"),
        ),
    ]

    return {
        "respondent_count": respondent_count,
        "questions": questions,
    }


def get_compatibility_tally():
    """Average valid student scores for approved proposals.

    Compatibility ties are determined by rounding averages to two decimal
    places with ROUND_HALF_UP, matching the individual score precision.
    """

    proposals = list(
        ShirtProposal.objects
        .filter(
            status=ShirtProposal.Status.APPROVED,
            is_invalidated=False,
        )
        .order_by("pk")
    )

    responses = list(
        PreferenceResponse.objects
        .filter(
            is_invalidated=False,
            participation_identity__is_eligible=True,
        )
    )

    results = []

    for proposal in proposals:
        scores = []

        for response in responses:
            score, _comparable_criteria = calculate_compatibility(
                response,
                proposal,
            )

            if score is not None:
                scores.append(Decimal(str(score)))

        mean = (
            sum(scores, Decimal("0")) / len(scores)
            if scores
            else None
        )

        results.append({
            "proposal_id": proposal.pk,
            "title": proposal.title,
            "valid_assessments": len(scores),
            "average_compatibility": mean,
            "status": "available" if scores else "insufficient_data",
        })

    available_results = [
        result for result in results
        if result["average_compatibility"] is not None
    ]

    highest_comparison_score = max(
        (
            result["average_compatibility"].quantize(
                COMPATIBILITY_TIE_QUANTUM,
                rounding=ROUND_HALF_UP,
            )
            for result in available_results
        ),
        default=None,
    )

    winners = [
        result for result in available_results
        if result["average_compatibility"].quantize(
            COMPATIBILITY_TIE_QUANTUM,
            rounding=ROUND_HALF_UP,
        ) == highest_comparison_score
    ]

    return {
        "status": "available" if available_results else "insufficient_data",
        "proposals": results,
        "winners": winners,
        "is_tie": len(winners) > 1,
        "tie_precision": "0.01",
    }


def is_results_release_authorized():
    """Return whether an authorized administrator enabled result release."""

    settings = ElectionSettings.objects.filter(pk=1).first()

    return settings is not None and settings.results_release_authorized


def create_result_snapshot(actor, trigger):
    """Append an immutable snapshot of the currently released result set."""
    archived_names = []
    archive_storage = FileSystemStorage(
        location=Path(django_settings.PRIVATE_MEDIA_ROOT)
        / "election_result_snapshots",
    )
    try:
        with transaction.atomic():
            election_settings = (
                ElectionSettings.objects.select_for_update()
                .filter(pk=1)
                .first()
            )
            if (
                election_settings is None
                or election_settings.voting_open
                or not election_settings.results_release_authorized
            ):
                raise ValidationError(
                    "Só é possível versionar resultados divulgados após o encerramento."
                )

            vote_tally = get_vote_tally()
            compatibility_tally = get_compatibility_tally()
            compatibility_by_proposal = {
                item["proposal_id"]: item
                for item in compatibility_tally["proposals"]
            }
            proposals = []
            image_manifest = {}
            for result in vote_tally["proposals"]:
                proposal_id = result["proposal_id"]
                compatibility = compatibility_by_proposal[proposal_id]
                proposal = ShirtProposal.objects.get(pk=proposal_id)
                image_url = reverse(
                    "result-snapshot-image",
                    args=[0, proposal_id],
                )
                proposals.append({
                    **result,
                    "image_url": image_url,
                    "compatibility": compatibility["average_compatibility"],
                    "valid_assessments": compatibility["valid_assessments"],
                })

            payload = {
                "status": vote_tally["status"],
                "total_votes": vote_tally["total_votes"],
                "excluded_votes": vote_tally["excluded_votes"],
                "excluded_proposals": vote_tally["excluded_proposals"],
                "proposals": proposals,
                "winners": vote_tally["winners"],
                "is_tie": vote_tally["is_tie"],
                "survey_results": get_preference_tally(),
                "compatibility_status": compatibility_tally["status"],
                "compatibility_winners": compatibility_tally["winners"],
                "compatibility_is_tie": compatibility_tally["is_tie"],
            }
            latest = ElectionResultSnapshot.objects.order_by(
                "-sequence"
            ).first()
            sequence = latest.sequence + 1 if latest else 1

            for proposal in ShirtProposal.objects.filter(
                pk__in=[
                    result["proposal_id"]
                    for result in vote_tally["proposals"]
                ]
            ):
                if not proposal.image or not proposal.image.name:
                    continue
                with proposal.image.storage.open(proposal.image.name, "rb") as source:
                    image_bytes = source.read()
                image_format = get_proposal_image_format(
                    ContentFile(image_bytes)
                )
                content_type, extension = {
                    "JPEG": ("image/jpeg", "jpg"),
                    "PNG": ("image/png", "png"),
                    "WEBP": ("image/webp", "webp"),
                }.get(image_format, (None, None))
                if content_type is None:
                    raise ValidationError("Formato de imagem de proposta inválido.")
                digest = hashlib.sha256(image_bytes).hexdigest()
                name = archive_storage.save(
                    f"version-{sequence}/proposal-{proposal.pk}.{extension}",
                    ContentFile(image_bytes),
                )
                archived_names.append(name)
                image_manifest[str(proposal.pk)] = {
                    "name": name,
                    "sha256": digest,
                    "content_type": content_type,
                }

            for item in payload["proposals"]:
                item["image_url"] = reverse(
                    "result-snapshot-image",
                    args=[sequence, item["proposal_id"]],
                )

            payload = json.loads(
                json.dumps(payload, cls=DjangoJSONEncoder)
            )
            encoded_payload = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            payload_digest = hashlib.sha256(encoded_payload).hexdigest()
            state_payload = json.loads(json.dumps(payload))
            for item in state_payload["proposals"]:
                item.pop("image_url", None)
            state_assets = {
                proposal_id: value["sha256"]
                for proposal_id, value in image_manifest.items()
            }
            state_digest = hashlib.sha256(
                json.dumps(
                    {"payload": state_payload, "assets": state_assets},
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()

            if latest and latest.state_sha256 == state_digest:
                for name in archived_names:
                    archive_storage.delete(name)
                return latest

            snapshot = ElectionResultSnapshot(
                sequence=sequence,
                payload=payload,
                image_manifest=image_manifest,
                payload_sha256=payload_digest,
                state_sha256=state_digest,
                actor=actor if getattr(actor, "is_authenticated", False) else None,
                actor_identifier=(
                    actor.pk if getattr(actor, "is_authenticated", False) else None
                ),
                trigger=trigger,
                previous=latest,
            )
            snapshot.save()
            return snapshot
    except Exception:
        for name in archived_names:
            archive_storage.delete(name)
        raise


def get_result_snapshot(sequence):
    return ElectionResultSnapshot.objects.filter(sequence=sequence).first()