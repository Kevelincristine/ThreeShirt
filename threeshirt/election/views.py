import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import (
    FileResponse,
    Http404,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.admin import three_shirt_admin
from accounts.models import UserProfile

from .models import ElectionSettings, ShirtProposal, Vote
from .services import (
    get_approved_proposals,
    get_preference_response,
    is_results_release_authorized,
    is_survey_open,
)
from .validators import get_proposal_image_format

logger = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


@require_GET
@never_cache
def student_dashboard_status(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Autenticação necessária."},
            status=401,
        )

    try:
        preferences = get_preference_response(request.user)
        settings = ElectionSettings.objects.filter(pk=1).first()
        response = {
            "survey_open": is_survey_open(),
            "voting_open": settings is not None and settings.voting_open,
            "results_release_authorized": is_results_release_authorized(),
            "has_preferences": preferences is not None,
            "has_voted": Vote.objects.filter(
                student=request.user,
            ).exists(),
        }
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except DatabaseError:
        logger.exception("Falha ao consultar o estado do painel do estudante.")
        return JsonResponse(
            {"error": "Não foi possível consultar o painel."},
            status=500,
        )

    return JsonResponse(response)


@require_GET
@never_cache
def approved_student_proposals(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Autenticação necessária."},
            status=401,
        )

    try:
            proposals = list(get_approved_proposals(request.user))
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except DatabaseError:
        logger.exception("Falha ao consultar propostas aprovadas.")
        return JsonResponse(
            {"error": "Não foi possível consultar as propostas."},
            status=500,
        )

    return JsonResponse({
        "proposals": [
            {
                "id": proposal.pk,
                "title": proposal.title,
                "description": proposal.description,
                "image_url": reverse(
                    "proposal-image",
                    args=[proposal.pk],
                ),
                "base_color": proposal.base_color,
                "related_to_course": proposal.related_to_course,
                "animal": proposal.animal,
                "additional_colors": proposal.additional_colors,
                "has_collar": proposal.has_collar,
                "has_phrase": proposal.has_phrase,
                "phrase_text": proposal.phrase_text,
            }
            for proposal in proposals
        ],
    })


@require_GET
@never_cache
def student_preferences(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Autenticação necessária."},
            status=401,
        )

    try:
        preferences = get_preference_response(request.user)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except DatabaseError:
        logger.exception("Falha ao consultar as preferências do estudante.")
        return JsonResponse(
            {"error": "Não foi possível consultar suas preferências."},
            status=500,
        )

    if preferences is None:
        return JsonResponse({
            "has_preferences": False,
            "preferences": None,
        })

    return JsonResponse({
        "has_preferences": True,
        "preferences": {
            "base_color": preferences.base_color,
            "related_to_course": preferences.related_to_course,
            "wants_animal": preferences.wants_animal,
            "preferred_animal": preferences.preferred_animal,
            "preferred_colors": preferences.preferred_colors,
            "wants_collar": preferences.wants_collar,
            "wants_phrase": preferences.wants_phrase,
            "intends_to_buy": preferences.intends_to_buy,
        },
    })


def _is_authorized_student(user):
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        return False

    try:
        return user.profile.role == UserProfile.Role.STUDENT
    except UserProfile.DoesNotExist:
        return False


def _is_authorized_admin(request):
    if not three_shirt_admin.has_permission(request):
        return False

    try:
        return request.user.profile.role == UserProfile.Role.ADMIN
    except UserProfile.DoesNotExist:
        return False


@require_GET
def proposal_image(request, proposal_id):
    proposal = get_object_or_404(ShirtProposal, pk=proposal_id)

    if proposal.status == ShirtProposal.Status.PENDING:
        if not _is_authorized_admin(request):
            return HttpResponseForbidden()
    elif proposal.status == ShirtProposal.Status.APPROVED:
        if (
            not _is_authorized_admin(request)
            and not _is_authorized_student(request.user)
        ):
            return HttpResponseForbidden()
    else:
        raise Http404

    try:
        image_file = proposal.image.storage.open(proposal.image.name, "rb")
    except FileNotFoundError as exc:
        raise Http404 from exc

    keep_file_open = False
    try:
        image_format = get_proposal_image_format(image_file)
        image_file.seek(0)
        content_type_and_extension = IMAGE_CONTENT_TYPES.get(image_format)
        if content_type_and_extension is None:
            raise Http404

        content_type, extension = content_type_and_extension
        response = FileResponse(
            image_file,
            as_attachment=False,
            filename=f"proposal-{proposal.pk}.{extension}",
            content_type=content_type,
        )
        response["X-Content-Type-Options"] = "nosniff"
        keep_file_open = True
        return response
    except ValidationError as exc:
        raise Http404 from exc
    finally:
        if not keep_file_open:
            image_file.close()
