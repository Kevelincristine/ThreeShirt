import json
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
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)

from accounts.admin import three_shirt_admin
from accounts.models import UserProfile

from .models import ElectionSettings, ShirtProposal, Vote
from .services import (
    get_approved_proposals,
    get_compatibility_tally,
    get_preference_response,
    get_vote_tally,
    is_results_release_authorized,
    is_survey_open,
    cast_vote,
    save_preference_response,
    submit_shirt_proposal,
)
from .validators import get_proposal_image_format

logger = logging.getLogger(__name__)

IMAGE_CONTENT_TYPES = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}


def _validation_error_response(error):
    errors = getattr(error, "message_dict", None)
    if errors is None:
        errors = {"form": error.messages}
    return JsonResponse(
        {"error": "Verifique os dados enviados.", "fields": errors},
        status=400,
    )


def _preference_payload(preferences):
    return {
        "base_color": preferences.base_color,
        "related_to_course": preferences.related_to_course,
        "wants_animal": preferences.wants_animal,
        "preferred_animal": preferences.preferred_animal,
        "preferred_colors": preferences.preferred_colors,
        "wants_collar": preferences.wants_collar,
        "wants_phrase": preferences.wants_phrase,
        "intends_to_buy": preferences.intends_to_buy,
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


@require_http_methods(["GET", "POST"])
@never_cache
def student_preferences(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Autenticação necessária."},
            status=401,
        )

    try:
        if request.method == "POST":
            try:
                data = json.loads(request.body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JsonResponse(
                    {"error": "O corpo da solicitação deve conter JSON válido."},
                    status=400,
                )
            preferences = save_preference_response(request.user, data)
            return JsonResponse({
                "message": "Preferências salvas com sucesso.",
                "has_preferences": True,
                "preferences": _preference_payload(preferences),
            })

        preferences = get_preference_response(request.user)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except ValidationError as error:
        return _validation_error_response(error)
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
        "preferences": _preference_payload(preferences),
    })


@require_http_methods(["GET", "POST"])
@never_cache
def student_submissions(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Autenticação necessária."}, status=401)

    try:
        get_preference_response(request.user)
        if request.method == "POST":
            data = request.POST.dict()
            for field in ("related_to_course", "has_collar", "has_phrase"):
                value = data.get(field)
                if value not in ("true", "false"):
                    raise ValidationError({field: "Informe sim ou não."})
                data[field] = value == "true"

            try:
                data["additional_colors"] = json.loads(
                    data.get("additional_colors", "[]")
                )
            except json.JSONDecodeError as exc:
                raise ValidationError({
                    "additional_colors": "Informe uma lista JSON válida."
                }) from exc

            data["image"] = request.FILES.get("image")
            proposal = submit_shirt_proposal(request.user, data)
            return JsonResponse(
                {
                    "message": "Proposta enviada para aprovação.",
                    "proposal": {
                        "id": proposal.pk,
                        "title": proposal.title,
                        "description": proposal.description,
                        "status": proposal.status,
                    },
                },
                status=201,
            )

        proposals = ShirtProposal.objects.filter(
            author=request.user,
        ).values("id", "title", "description", "status", "created_at")
        return JsonResponse({
            "proposals": [
                {
                    **proposal,
                    "created_at": proposal["created_at"].isoformat(),
                }
                for proposal in proposals
            ],
        })
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except ValidationError as error:
        return _validation_error_response(error)
    except DatabaseError:
        logger.exception("Falha ao consultar ou enviar propostas do estudante.")
        return JsonResponse(
            {"error": "Não foi possível concluir a operação de proposta."},
            status=500,
        )


@require_POST
@never_cache
def student_vote(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Autenticação necessária."}, status=401)

    try:
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {"error": "O corpo da solicitação deve conter JSON válido."},
                status=400,
            )
        if (
            not isinstance(data, dict)
            or type(data.get("proposal_id")) is not int
        ):
            raise ValidationError({
                "proposal_id": "Informe uma proposta válida."
            })

        vote = cast_vote(request.user, data["proposal_id"])
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except ValidationError as error:
        return _validation_error_response(error)
    except DatabaseError:
        logger.exception("Falha ao registrar o voto do estudante.")
        return JsonResponse(
            {"error": "Não foi possível registrar o voto."},
            status=500,
        )

    return JsonResponse({
        "message": "Voto registrado com sucesso.",
        "has_voted": True,
        "created_at": vote.created_at.isoformat(),
    })


@require_GET
@never_cache
def student_results(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Autenticação necessária."}, status=401)

    try:
        get_preference_response(request.user)
        settings = ElectionSettings.objects.filter(pk=1).first()
        if not is_results_release_authorized():
            return JsonResponse(
                {"error": "A divulgação dos resultados ainda não foi autorizada."},
                status=403,
            )
        if settings is None or settings.voting_open:
            return JsonResponse(
                {"error": "Os resultados estarão disponíveis após o encerramento da votação."},
                status=409,
            )

        vote_tally = get_vote_tally()
        compatibility_tally = get_compatibility_tally()
        compatibility_by_proposal = {
            result["proposal_id"]: result
            for result in compatibility_tally["proposals"]
        }
        proposals = []
        for result in vote_tally["proposals"]:
            compatibility = compatibility_by_proposal[result["proposal_id"]]
            proposals.append({
                **result,
                "compatibility": compatibility["average_compatibility"],
                "valid_assessments": compatibility["valid_assessments"],
            })
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except DatabaseError:
        logger.exception("Falha ao apurar resultados para estudantes.")
        return JsonResponse(
            {"error": "Não foi possível consultar os resultados."},
            status=500,
        )

    return JsonResponse({
        "status": vote_tally["status"],
        "total_votes": vote_tally["total_votes"],
        "proposals": proposals,
        "winners": vote_tally["winners"],
        "is_tie": vote_tally["is_tie"],
        "compatibility_status": compatibility_tally["status"],
        "compatibility_winners": compatibility_tally["winners"],
        "compatibility_is_tie": compatibility_tally["is_tie"],
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
