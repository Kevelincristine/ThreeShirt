"""Business logic for comparing student preferences with shirt proposals."""

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count

from accounts.admin import three_shirt_admin
from accounts.models import UserProfile

from .models import (
    COLOR_CODES,
    ElectionSettings,
    PreferenceResponse,
    ShirtProposal,
    Vote,
)
from .concurrency import run_with_sqlite_lock_retry

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
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        raise PermissionDenied("É necessário um usuário ativo.")

    try:
        profile = user.profile
    except UserProfile.DoesNotExist as exc:
        raise PermissionDenied(
            "O usuário precisa ter um perfil de estudante."
        ) from exc

    if profile.role != UserProfile.Role.STUDENT:
        raise PermissionDenied(
            "Somente estudantes podem responder à pesquisa."
        )


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
        if proposal.status != ShirtProposal.Status.APPROVED:
            raise ValidationError({
                "proposal": "Só é possível votar em propostas aprovadas."
            })

        if Vote.objects.filter(student=user).exists():
            raise ValidationError({
                "vote": "Você já registrou seu voto."
            })

        try:
            with transaction.atomic():
                vote = Vote.objects.create(
                    student=user,
                    proposal=proposal,
                )
        except IntegrityError as exc:
            if Vote.objects.filter(student=user).exists():
                raise ValidationError({
                    "vote": "Você já registrou seu voto."
                }) from exc
            raise

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

        response = (
            PreferenceResponse.objects
            .select_for_update()
            .filter(student=user)
            .first()
        )
        if response is None:
            response = PreferenceResponse(student=user)

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

    proposal = ShirtProposal(
        author=user,
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
    return PreferenceResponse.objects.filter(student=user).first()


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
        if isinstance(preference_value, bool) and isinstance(proposal_value, bool):
            similarities.append(float(preference_value == proposal_value))

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

    score = round(sum(similarities) / comparable_criteria * 100, 2)
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
        .filter(status=ShirtProposal.Status.PENDING)
        .values("id", "title", "description")
    )


def get_approved_proposals(user):
    """Return approved proposals to an active student, newest first."""
    _require_student_user(user)
    return ShirtProposal.objects.filter(
        status=ShirtProposal.Status.APPROVED,
    ).order_by("-created_at", "-pk")


def get_vote_tally():
    """Count approved-proposal votes; report other vote records separately.

    Votes for proposals that are no longer approved remain in the database
    but are excluded from the valid-vote denominator and winner calculation.
    """
    proposal_counts = list(
        ShirtProposal.objects
        .values("pk", "title", "status")
        .annotate(vote_count=Count("votes"))
        .order_by("pk")
    )
    approved_proposals = [
        proposal for proposal in proposal_counts
        if proposal["status"] == ShirtProposal.Status.APPROVED
    ]
    excluded_proposals = [
        {
            "proposal_id": proposal["pk"],
            "title": proposal["title"],
            "status": proposal["status"],
            "vote_count": proposal["vote_count"],
        }
        for proposal in proposal_counts
        if (
            proposal["status"] != ShirtProposal.Status.APPROVED
            and proposal["vote_count"]
        )
    ]
    total_votes = sum(
        proposal["vote_count"] for proposal in approved_proposals
    )

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
        "excluded_votes": sum(
            proposal["vote_count"] for proposal in excluded_proposals
        ),
        "excluded_proposals": excluded_proposals,
        "proposals": results,
        "winners": winners,
        "is_tie": len(winners) > 1,
    }


def get_compatibility_tally():
    """Average valid student scores for approved proposals.

    Compatibility ties are determined by rounding averages to two decimal
    places with ROUND_HALF_UP, matching the individual score precision.
    """
    proposals = list(
        ShirtProposal.objects
        .filter(status=ShirtProposal.Status.APPROVED)
        .order_by("pk")
    )
    responses = list(
        PreferenceResponse.objects
        .filter(
            student__is_active=True,
            student__profile__role=UserProfile.Role.STUDENT,
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
