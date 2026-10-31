import json
import hashlib
import logging
import secrets
from datetime import timedelta

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.http import (
    FileResponse,
    Http404,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import (
    require_GET,
    require_http_methods,
    require_POST,
)

from accounts.models import (
    EmailLoginCode,
    LoginRequestRateBucket,
    UserProfile,
)

from .models import (
    ElectionSettings,
    MaintenanceAudit,
    MaintenanceConfirmation,
    MaintenanceFileCleanup,
    PreferenceResponse,
    ShirtProposal,
    Vote,
)
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

MAINTENANCE_CONFIRMATION_TTL = timedelta(minutes=10)
ELECTION_RESET_CONFIRMATION = "REINICIAR ELEIÇÃO"
MAINTENANCE_ACTIONS = {
    "delete_user": MaintenanceConfirmation.Action.DELETE_USER,
    "delete_proposal": MaintenanceConfirmation.Action.DELETE_PROPOSAL,
    "reset_election": MaintenanceConfirmation.Action.RESET_ELECTION,
}

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


User = get_user_model()


def _is_active_admin_user(user):
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        return False
    try:
        return user.profile.role in UserProfile.ADMIN_ROLES
    except UserProfile.DoesNotExist:
        return False


def _require_admin_user(request):
    if not _is_active_admin_user(request.user):
        raise PermissionDenied("Permissão administrativa necessária.")
    return request.user


def _serialize_user_summary(user):
    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        profile = None
    return {
        "id": user.pk,
        "email": user.email,
        "full_name": (profile.full_name if profile else ""),
        "role": profile.role if profile else UserProfile.Role.STUDENT,
        "is_active": bool(user.is_active),
        "is_staff": bool(user.is_staff),
    }


def _is_active_maintenance_admin(user):
    if (
        not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
    ):
        return False
    try:
        return user.profile.role == UserProfile.Role.ADMIN
    except UserProfile.DoesNotExist:
        return False


def _snapshot_digest(snapshot):
    serialized = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _timestamp_snapshot(value):
    return value.isoformat() if value is not None else None


def _check_account_deletion_allowed(user, actor):
    try:
        role = user.profile.role
    except UserProfile.DoesNotExist:
        role = None

    if user.is_active and role == UserProfile.Role.ADMIN:
        remaining_admins = User.objects.filter(
            is_active=True,
            profile__role=UserProfile.Role.ADMIN,
        ).exclude(pk=user.pk).count()
        if remaining_admins == 0:
            raise ValidationError(
                "Não é possível excluir a última conta administrativa ativa."
            )

    if user.pk == actor.pk:
        raise ValidationError("Não é possível excluir a própria conta.")


def _build_user_deletion_plan(target_id, actor):
    user = User.objects.filter(pk=target_id).first()
    if user is None:
        raise User.DoesNotExist

    _check_account_deletion_allowed(user, actor)
    profiles = list(
        UserProfile.objects.filter(user_id=user.pk)
        .values("pk", "role", "full_name")
    )
    preferences = list(
        PreferenceResponse.objects.filter(student_id=user.pk)
        .values_list("pk", flat=True)
    )
    proposals = list(
        ShirtProposal.objects.filter(author_id=user.pk)
        .values("pk", "status", "image", "reviewed_by_id")
    )
    proposal_ids = [proposal["pk"] for proposal in proposals]
    votes = list(
        Vote.objects.filter(
            Q(student_id=user.pk) | Q(proposal_id__in=proposal_ids)
        )
        .order_by("pk")
        .values("pk", "student_id", "proposal_id")
    )
    shared_email = (
        bool(user.email)
        and User.objects.filter(email__iexact=user.email)
        .exclude(pk=user.pk)
        .exists()
    )
    login_codes = (
        []
        if shared_email or not user.email
        else list(
            EmailLoginCode.objects.filter(email__iexact=user.email)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    )
    group_ids = list(user.groups.order_by("pk").values_list("pk", flat=True))
    permission_ids = list(
        user.user_permissions.order_by("pk").values_list("pk", flat=True)
    )
    admin_log_ids = list(
        LogEntry.objects.filter(user_id=user.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    image_names = sorted({
        proposal["image"] for proposal in proposals if proposal["image"]
    })
    remaining_image_refs = list(
        ShirtProposal.objects.filter(image__in=image_names)
        .exclude(pk__in=proposal_ids)
        .order_by("pk")
        .values("pk", "image")
    )
    remaining_image_names = {
        reference["image"] for reference in remaining_image_refs
    }
    files_to_remove = [
        name for name in image_names if name not in remaining_image_names
    ]

    summary = {
        "accounts": 1,
        "profiles": len(profiles),
        "preference_responses": len(preferences),
        "proposals": len(proposals),
        "votes": len(votes),
        "votes_from_other_accounts": sum(
            vote["student_id"] != user.pk for vote in votes
        ),
        "email_login_codes": len(login_codes),
        "group_memberships": len(group_ids),
        "direct_permissions": len(permission_ids),
        "admin_log_entries": len(admin_log_ids),
        "proposal_images": len(image_names),
        "files_to_remove": len(files_to_remove),
    }
    snapshot = {
        "user": {
            "pk": user.pk,
            "username": user.get_username(),
            "email": user.email,
            "is_active": user.is_active,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "last_login": _timestamp_snapshot(user.last_login),
        },
        "profiles": profiles,
        "preferences": preferences,
        "proposals": proposals,
        "votes": votes,
        "login_codes": login_codes,
        "groups": group_ids,
        "permissions": permission_ids,
        "admin_logs": admin_log_ids,
        "shared_email": shared_email,
        "remaining_image_refs": remaining_image_refs,
    }
    full_name = profiles[0]["full_name"] if profiles else ""
    target = {
        "id": user.pk,
        "label": full_name or user.email or user.get_username(),
        "email": user.email,
    }
    image_storage = ShirtProposal._meta.get_field("image").storage
    files = [(image_storage, name) for name in files_to_remove]
    warnings = []
    if shared_email:
        warnings.append(
            "Códigos de acesso não serão removidos porque outra conta usa "
            "o mesmo e-mail."
        )
    if remaining_image_refs:
        warnings.append(
            "Imagens ainda referenciadas por outras propostas serão preservadas."
        )
    return {
        "target_id": user.pk,
        "target": target,
        "summary": summary,
        "snapshot_digest": _snapshot_digest(snapshot),
        "audit_counts": summary,
        "files": files,
        "warning": " ".join(warnings),
    }


def _build_proposal_deletion_plan(target_id):
    proposal = (
        ShirtProposal.objects.select_related("author", "author__profile")
        .filter(pk=target_id)
        .first()
    )
    if proposal is None:
        raise ShirtProposal.DoesNotExist

    votes = list(
        Vote.objects.filter(proposal_id=proposal.pk)
        .order_by("pk")
        .values("pk", "student_id")
    )
    settings = ElectionSettings.objects.filter(pk=1).first()
    voting_open = bool(settings and settings.voting_open)
    image_names = [proposal.image.name] if proposal.image.name else []
    remaining_image_refs = list(
        ShirtProposal.objects.filter(image__in=image_names)
        .exclude(pk=proposal.pk)
        .order_by("pk")
        .values("pk", "image")
    ) if image_names else []
    files_to_remove = (
        image_names
        if not remaining_image_refs
        else []
    )
    summary = {
        "proposals": 1,
        "votes": len(votes),
        "proposal_images": len(image_names),
        "files_to_remove": len(files_to_remove),
        "votes_from_other_accounts": sum(
            vote["student_id"] != proposal.author_id for vote in votes
        ),
    }
    profile = getattr(proposal.author, "profile", None)
    target = {
        "id": proposal.pk,
        "label": proposal.title,
        "status": proposal.status,
        "author": (
            (profile.full_name or proposal.author.email)
            if profile
            else proposal.author.email
        ),
    }
    snapshot = {
        "proposal": {
            "pk": proposal.pk,
            "title": proposal.title,
            "status": proposal.status,
            "author_id": proposal.author_id,
            "reviewed_by_id": proposal.reviewed_by_id,
            "image": proposal.image.name,
            "updated_at": _timestamp_snapshot(proposal.updated_at),
        },
        "votes": votes,
        "remaining_image_refs": remaining_image_refs,
        "voting_open": voting_open,
    }
    warning = ""
    if proposal.status == ShirtProposal.Status.APPROVED:
        warning = (
            "A proposta aprovada será removida da eleição e os votos "
            "associados serão excluídos."
        )
        if voting_open:
            warning += " A votação está aberta."
    files = [
        (ShirtProposal._meta.get_field("image").storage, name)
        for name in files_to_remove
    ]
    if remaining_image_refs:
        warning += " A imagem será preservada porque outra proposta ainda a utiliza."
    return {
        "target_id": proposal.pk,
        "target": target,
        "summary": summary,
        "snapshot_digest": _snapshot_digest(snapshot),
        "audit_counts": summary,
        "files": files,
        "warning": warning,
    }


def _build_election_reset_plan():
    proposals = [
        {
            **proposal,
            "updated_at": _timestamp_snapshot(proposal["updated_at"]),
        }
        for proposal in ShirtProposal.objects.order_by("pk").values(
            "pk",
            "status",
            "image",
            "updated_at",
        )
    ]
    preferences = list(
        PreferenceResponse.objects.order_by("pk")
        .values_list("pk", flat=True)
    )
    votes = list(
        Vote.objects.order_by("pk")
        .values("pk", "student_id", "proposal_id")
    )
    settings = ElectionSettings.objects.filter(pk=1).first()
    settings_snapshot = None
    if settings is not None:
        settings_snapshot = {
            "pk": settings.pk,
            "survey_open": settings.survey_open,
            "proposal_submission_open": settings.proposal_submission_open,
            "voting_open": settings.voting_open,
            "results_release_authorized": settings.results_release_authorized,
            "survey_closed_at": _timestamp_snapshot(settings.survey_closed_at),
            "voting_started_at": _timestamp_snapshot(settings.voting_started_at),
            "voting_ended_at": _timestamp_snapshot(settings.voting_ended_at),
            "compatibility_weight": settings.compatibility_weight,
        }
    image_names = sorted({
        proposal["image"] for proposal in proposals if proposal["image"]
    })
    summary = {
        "votes": len(votes),
        "preference_responses": len(preferences),
        "proposals": len(proposals),
        "proposal_images": len(image_names),
        "files_to_remove": len(image_names),
        "settings_rows": int(settings is not None),
        "accounts_preserved": User.objects.count(),
        "profiles_preserved": UserProfile.objects.count(),
        "email_login_codes_preserved": EmailLoginCode.objects.count(),
        "login_rate_buckets_preserved": LoginRequestRateBucket.objects.count(),
    }
    snapshot = {
        "proposals": proposals,
        "preferences": preferences,
        "votes": votes,
        "settings": settings_snapshot,
        "preserved": {
            "accounts": summary["accounts_preserved"],
            "profiles": summary["profiles_preserved"],
            "email_login_codes": summary["email_login_codes_preserved"],
            "login_rate_buckets": summary["login_rate_buckets_preserved"],
        },
    }
    return {
        "target_id": None,
        "target": {"label": "Todos os dados eleitorais"},
        "summary": summary,
        "snapshot_digest": _snapshot_digest(snapshot),
        "audit_counts": {
            key: value
            for key, value in summary.items()
            if not key.endswith("_preserved")
        },
        "files": [
            (ShirtProposal._meta.get_field("image").storage, name)
            for name in image_names
        ],
        "warning": (
            "A eleição será interrompida e os resultados calculados "
            "deixarão de estar disponíveis."
            if settings and settings.voting_open
            else ""
        ),
    }


def _build_maintenance_plan(action, target_id, actor):
    if action == MaintenanceConfirmation.Action.DELETE_USER:
        return _build_user_deletion_plan(target_id, actor)
    if action == MaintenanceConfirmation.Action.DELETE_PROPOSAL:
        return _build_proposal_deletion_plan(target_id)
    return _build_election_reset_plan()


def _apply_maintenance_action(action, target_id, plan, actor):
    for _storage, name in plan["files"]:
        MaintenanceFileCleanup.objects.get_or_create(storage_name=name)

    if action == MaintenanceConfirmation.Action.DELETE_USER:
        MaintenanceConfirmation.objects.filter(
            action=MaintenanceConfirmation.Action.DELETE_USER,
            target_id=target_id,
        ).delete()
        user = User.objects.get(pk=target_id)
        _check_account_deletion_allowed(user, actor)
        Vote.objects.filter(
            Q(student_id=target_id) | Q(proposal__author_id=target_id)
        ).delete()
        ShirtProposal.objects.filter(author_id=target_id).delete()
        if plan["summary"]["email_login_codes"]:
            EmailLoginCode.objects.filter(email__iexact=user.email).delete()
        user.delete()
    elif action == MaintenanceConfirmation.Action.DELETE_PROPOSAL:
        MaintenanceConfirmation.objects.filter(
            action=MaintenanceConfirmation.Action.DELETE_PROPOSAL,
            target_id=target_id,
        ).delete()
        Vote.objects.filter(proposal_id=target_id).delete()
        ShirtProposal.objects.filter(pk=target_id).delete()
    else:
        MaintenanceConfirmation.objects.all().delete()
        Vote.objects.all().delete()
        PreferenceResponse.objects.all().delete()
        ShirtProposal.objects.all().delete()
        settings = ElectionSettings.objects.filter(pk=1).first()
        if settings is None:
            settings = ElectionSettings(pk=1)
        settings.survey_open = False
        settings.proposal_submission_open = True
        settings.voting_open = False
        settings.results_release_authorized = False
        settings.survey_closed_at = None
        settings.voting_started_at = None
        settings.voting_ended_at = None
        settings.compatibility_weight = 50
        settings.full_clean()
        settings.save()
def _process_maintenance_file_cleanup():
    storage = ShirtProposal._meta.get_field("image").storage
    pending = list(MaintenanceFileCleanup.objects.order_by("pk"))
    failures = 0
    for item in pending:
        try:
            storage.delete(item.storage_name)
        except OSError:
            failures += 1
            logger.exception(
                "Falha ao remover arquivo após manutenção eleitoral."
            )
            continue
        try:
            item.delete()
        except DatabaseError:
            failures += 1
            logger.exception(
                "Falha ao registrar a limpeza do arquivo de manutenção."
            )
    return failures


@require_GET
@never_cache
def admin_dashboard(request):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    settings = ElectionSettings.objects.filter(pk=1).first()
    active_students = User.objects.filter(
        is_active=True,
        profile__role__in=UserProfile.STUDENT_ROLES,
    ).count()
    total_active_users = User.objects.filter(is_active=True).count()
    responses = PreferenceResponse.objects.filter(
        student__is_active=True,
        student__profile__role__in=UserProfile.STUDENT_ROLES,
    ).values_list("student_id", flat=True).distinct().count()
    proposal_counts = {
        status: ShirtProposal.objects.filter(status=status).count()
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.APPROVED,
            ShirtProposal.Status.REJECTED,
        )
    }

    return JsonResponse({
        "survey_open": bool(settings and settings.survey_open),
        "proposal_submission_open": bool(
            settings is not None and settings.proposal_submission_open is not False
        ),
        "voting_open": bool(settings and settings.voting_open),
        "results_release_authorized": bool(
            settings and settings.results_release_authorized
        ),
        "counts": {
            "students_total": active_students,
            "students_active": active_students,
            "users_total": total_active_users,
            "proposals_pending": proposal_counts[ShirtProposal.Status.PENDING],
            "proposals_approved": proposal_counts[ShirtProposal.Status.APPROVED],
            "proposals_rejected": proposal_counts[ShirtProposal.Status.REJECTED],
            "preferences_responses": responses,
            "votes_cast": Vote.objects.count(),
            "admins_total": User.objects.filter(
                is_active=True,
                profile__role__in=UserProfile.ADMIN_ROLES,
            ).count(),
        },
        "progress": {
            "survey_participation": (
                round((responses / active_students) * 100, 2)
                if active_students
                else None
            ),
            "survey_completed": responses,
        },
    })


@require_GET
@never_cache
def admin_proposals(request):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    proposals = ShirtProposal.objects.select_related(
        "author",
        "author__profile",
        "reviewed_by",
        "reviewed_by__profile",
    ).order_by("-created_at")
    payload = []
    for proposal in proposals:
        payload.append({
            "id": proposal.pk,
            "title": proposal.title,
            "description": proposal.description,
            "status": proposal.status,
            "base_color": proposal.base_color,
            "animal": proposal.animal,
            "additional_colors": proposal.additional_colors,
            "has_collar": proposal.has_collar,
            "has_phrase": proposal.has_phrase,
            "phrase_text": proposal.phrase_text,
            "created_at": proposal.created_at.isoformat(),
            "updated_at": proposal.updated_at.isoformat(),
            "reviewed_at": (
                proposal.reviewed_at.isoformat() if proposal.reviewed_at else None
            ),
            "rejection_reason": proposal.rejection_reason,
            "image_url": reverse("proposal-image", args=[proposal.pk]),
            "author": {
                "id": proposal.author_id,
                "email": proposal.author.email,
                "full_name": (
                    proposal.author.profile.full_name
                    if hasattr(proposal.author, "profile")
                    else ""
                ),
            },
            "reviewed_by": (
                {
                    "id": proposal.reviewed_by_id,
                    "email": proposal.reviewed_by.email,
                    "full_name": (
                        proposal.reviewed_by.profile.full_name
                        if hasattr(proposal.reviewed_by, "profile")
                        else ""
                    ),
                }
                if proposal.reviewed_by_id
                else None
            ),
        })
    return JsonResponse({"proposals": payload})


@require_http_methods(["POST"])
@never_cache
def admin_review_proposal(request, proposal_id):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )

    decision = payload.get("decision")
    if decision not in {"approve", "reject"}:
        return JsonResponse(
            {"error": "Decisão inválida. Use 'approve' ou 'reject'."},
            status=400,
        )

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        return JsonResponse(
            {"error": "O motivo deve ser um texto."},
            status=400,
        )
    reason = reason.strip()

    try:
        with transaction.atomic():
            settings = (
                ElectionSettings.objects
                .select_for_update()
                .filter(pk=1)
                .first()
            )
            if settings is not None and settings.voting_open:
                raise ValidationError({
                    "voting": "As propostas não podem ser revisadas durante a votação."
                })
            proposal = (
                ShirtProposal.objects
                .select_for_update()
                .select_related("author", "author__profile")
                .get(pk=proposal_id)
            )
            if proposal.status != ShirtProposal.Status.PENDING:
                raise ValidationError(
                    {"status": "A proposta não está pendente de revisão."}
                )
            if decision == "approve":
                proposal.status = ShirtProposal.Status.APPROVED
                proposal.rejection_reason = ""
                message = "Proposta aprovada com sucesso."
            else:
                proposal.status = ShirtProposal.Status.REJECTED
                proposal.rejection_reason = reason
                if not reason:
                    proposal.rejection_reason = "Motivo não informado."
                message = "Proposta rejeitada com sucesso."
            proposal.reviewed_by = request.user
            proposal.reviewed_at = timezone.now()
            try:
                proposal.full_clean()
            finally:
                proposal.image.close()
            proposal.save()
    except ShirtProposal.DoesNotExist:
        return JsonResponse({"error": "Proposta não encontrada."}, status=404)
    except ValidationError as exc:
        return _validation_error_response(exc)
    except DatabaseError:
        logger.exception("Falha ao revisar proposta administrativa.")
        return JsonResponse(
            {"error": "Não foi possível atualizar a proposta."},
            status=500,
        )

    return JsonResponse({
        "message": message,
        "proposal": {
            "id": proposal.pk,
            "title": proposal.title,
            "status": proposal.status,
            "reviewed_at": proposal.reviewed_at.isoformat(),
            "rejection_reason": proposal.rejection_reason,
        },
    })


@require_http_methods(["GET", "POST"])
@never_cache
def admin_stage_controls(request):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    settings, _created = ElectionSettings.objects.get_or_create(pk=1)
    if request.method == "GET":
        return JsonResponse({
            "survey_open": settings.survey_open,
            "proposal_submission_open": settings.proposal_submission_open is not False,
            "voting_open": settings.voting_open,
            "results_release_authorized": settings.results_release_authorized,
        })

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )

    stage = payload.get("stage")
    action = payload.get("action")
    if stage is None:
        for candidate in (
            "survey",
            "proposal_submission",
            "voting",
            "results_release",
        ):
            if candidate in payload:
                stage = candidate
                action = payload[candidate]
                break
    if stage not in {
        "survey",
        "proposal_submission",
        "voting",
        "results_release",
    }:
        return JsonResponse(
            {"error": "Etapa inválida. Use survey, proposal_submission, voting ou results_release."},
            status=400,
        )
    if action not in {"open", "close"}:
        return JsonResponse(
            {"error": "A ação deve ser 'open' ou 'close'."},
            status=400,
        )

    try:
        with transaction.atomic():
            settings = (
                ElectionSettings.objects
                .select_for_update()
                .filter(pk=1)
                .first()
            ) or settings
            changed = False
            if stage == "survey":
                next_value = action == "open"
                if settings.survey_open != next_value:
                    settings.survey_open = next_value
                    changed = True
                    if not next_value:
                        settings.survey_closed_at = timezone.now()
            elif stage == "proposal_submission":
                next_value = action == "open"
                if settings.proposal_submission_open != next_value:
                    settings.proposal_submission_open = next_value
                    changed = True
            elif stage == "results_release":
                next_value = action == "open"
                if settings.results_release_authorized != next_value:
                    settings.results_release_authorized = next_value
                    changed = True
            elif stage == "voting":
                if action == "open" and not settings.voting_open:
                    if Vote.objects.exists():
                        raise ValidationError({
                            "voting": "A votação não pode ser reaberta após o registro de votos."
                        })
                    if settings.survey_open:
                        raise ValidationError({
                            "survey": "Feche a pesquisa antes de abrir a votação."
                        })
                    if settings.proposal_submission_open is not False:
                        raise ValidationError({
                            "proposal_submission": "Feche o envio de propostas antes de abrir a votação."
                        })
                    settings.voting_open = True
                    settings.voting_started_at = timezone.now()
                    settings.voting_ended_at = None
                    changed = True
                elif action == "close" and settings.voting_open:
                    settings.voting_open = False
                    settings.voting_ended_at = timezone.now()
                    changed = True
            if changed:
                settings.full_clean()
                settings.save()
    except ValidationError as exc:
        return _validation_error_response(exc)
    except DatabaseError:
        logger.exception("Falha ao alterar a etapa administrativa.")
        return JsonResponse(
            {"error": "Não foi possível alterar o estado da etapa."},
            status=500,
        )

    return JsonResponse({
        "message": f"Etapa '{stage}' atualizada com sucesso.",
        "survey_open": settings.survey_open,
        "proposal_submission_open": settings.proposal_submission_open is not False,
        "voting_open": settings.voting_open,
        "results_release_authorized": settings.results_release_authorized,
    })


@require_GET
@never_cache
def admin_users(request):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    users = User.objects.select_related("profile").order_by("email")
    return JsonResponse({
        "users": [_serialize_user_summary(user) for user in users]
    })


@require_http_methods(["POST"])
@never_cache
def admin_user_permission(request, user_id):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )

    try:
        user = User.objects.select_related("profile").get(pk=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": "Usuário não encontrado."}, status=404)

    target_role = payload.get("role")
    if target_role is None and payload.get("is_admin") is not None:
        target_role = UserProfile.Role.ADMIN if payload.get("is_admin") else UserProfile.Role.STUDENT
    if target_role not in {
        UserProfile.Role.ADMIN,
        UserProfile.Role.STUDENT_ADMIN,
        UserProfile.Role.STUDENT,
    }:
        return JsonResponse(
            {"error": "Role inválida. Use 'STUDENT', 'STUDENT_ADMIN' ou 'ADMIN'."},
            status=400,
        )

    if user.pk == request.user.pk:
        return JsonResponse(
            {"error": "Você não pode alterar seu próprio papel."},
            status=400,
        )

    try:
        profile = user.profile
        current_role = profile.role
    except UserProfile.DoesNotExist:
        profile = None
        current_role = None
    if (
        user.is_active
        and current_role in UserProfile.ADMIN_ROLES
        and target_role not in UserProfile.ADMIN_ROLES
    ):
        remaining_admins = User.objects.filter(
            is_active=True,
            profile__role__in=UserProfile.ADMIN_ROLES,
        ).exclude(pk=user.pk).count()
        if remaining_admins == 0:
            return JsonResponse(
                {"error": "O sistema precisa manter pelo menos um administrador ativo."},
                status=400,
            )

    if profile is None:
        profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.role = target_role
    profile.save(update_fields=["role"])
    return JsonResponse({
        "message": "Permissão de usuário atualizada com sucesso.",
        "user": _serialize_user_summary(user),
    })


@require_http_methods(["POST"])
@never_cache
def admin_user_status(request, user_id):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )

    if "is_active" not in payload:
        return JsonResponse(
            {"error": "Informe o novo estado is_active."},
            status=400,
        )
    if not isinstance(payload["is_active"], bool):
        return JsonResponse(
            {"error": "is_active deve ser um valor booleano."},
            status=400,
        )

    try:
        user = User.objects.select_related("profile").get(pk=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": "Usuário não encontrado."}, status=404)

    if user.pk == request.user.pk and not payload["is_active"]:
        return JsonResponse(
            {"error": "Você não pode desativar a própria conta."},
            status=400,
        )
    try:
        current_role = user.profile.role
    except UserProfile.DoesNotExist:
        current_role = None
    if (
        user.is_active
        and current_role in UserProfile.ADMIN_ROLES
        and not payload["is_active"]
    ):
        remaining_admins = User.objects.filter(
            is_active=True,
            profile__role__in=UserProfile.ADMIN_ROLES,
        ).exclude(pk=user.pk).count()
        if remaining_admins == 0:
            return JsonResponse(
                {"error": "Não é possível deixar o sistema sem administradores ativos."},
                status=400,
            )

    if user.is_active != payload["is_active"]:
        user.is_active = payload["is_active"]
        user.save(update_fields=["is_active"])
    return JsonResponse({
        "message": "Status do usuário atualizado com sucesso.",
        "user": _serialize_user_summary(user),
    })


@require_POST
@never_cache
def admin_maintenance_preview(request):
    if not _is_active_maintenance_admin(request.user):
        return JsonResponse(
            {"error": "Apenas administradores ativos podem executar manutenção."},
            status=403,
        )

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"error": "O corpo da solicitação deve ser um objeto JSON."},
            status=400,
        )

    requested_action = payload.get("action")
    if not isinstance(requested_action, str):
        return JsonResponse(
            {"error": "Ação de manutenção inválida."},
            status=400,
        )
    action = MAINTENANCE_ACTIONS.get(requested_action)
    if action is None:
        return JsonResponse(
            {"error": "Ação de manutenção inválida."},
            status=400,
        )
    target_id = payload.get("target_id")
    if action == MaintenanceConfirmation.Action.RESET_ELECTION:
        if target_id is not None:
            return JsonResponse(
                {"error": "A reinicialização não aceita um identificador."},
                status=400,
            )
    elif type(target_id) is not int or target_id < 1:
        return JsonResponse(
            {"error": "Selecione um registro válido para exclusão."},
            status=400,
        )

    now = timezone.now()
    token = secrets.token_urlsafe(32)
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = now + MAINTENANCE_CONFIRMATION_TTL
    try:
        with transaction.atomic():
            MaintenanceConfirmation.objects.filter(
                expires_at__lte=now,
            ).delete()
            plan = _build_maintenance_plan(
                action,
                target_id,
                request.user,
            )
            confirmation = MaintenanceConfirmation.objects.create(
                actor=request.user,
                token_digest=token_digest,
                action=action,
                target_id=plan["target_id"],
                summary=plan["audit_counts"],
                snapshot_digest=plan["snapshot_digest"],
                expires_at=expires_at,
            )
    except (User.DoesNotExist, ShirtProposal.DoesNotExist):
        return JsonResponse(
            {"error": "O registro selecionado não foi encontrado."},
            status=404,
        )
    except ValidationError as exc:
        return _validation_error_response(exc)
    except DatabaseError:
        logger.exception("Falha ao preparar uma operação de manutenção.")
        return JsonResponse(
            {"error": "Não foi possível preparar a operação de manutenção."},
            status=500,
        )

    response = {
        "challenge_token": token,
        "expires_at": confirmation.expires_at.isoformat(),
        "action": action,
        "target": plan["target"],
        "summary": plan["summary"],
        "warning": plan["warning"],
    }
    if action == MaintenanceConfirmation.Action.RESET_ELECTION:
        response["restored_settings"] = {
            "survey_open": False,
            "proposal_submission_open": True,
            "voting_open": False,
            "results_release_authorized": False,
            "compatibility_weight": 50,
        }
    return JsonResponse(response)


@require_POST
@never_cache
def admin_maintenance_confirm(request):
    if not _is_active_maintenance_admin(request.user):
        return JsonResponse(
            {"error": "Apenas administradores ativos podem executar manutenção."},
            status=403,
        )

    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse(
            {"error": "O corpo da solicitação deve conter JSON válido."},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"error": "O corpo da solicitação deve ser um objeto JSON."},
            status=400,
        )

    token = payload.get("challenge_token")
    if not isinstance(token, str) or not token or len(token) > 256:
        return JsonResponse(
            {"error": "A confirmação não é válida ou expirou."},
            status=400,
        )
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    completed_action = None
    affected_counts = None
    warning = ""

    try:
        with transaction.atomic():
            confirmation = (
                MaintenanceConfirmation.objects.select_for_update()
                .filter(
                    token_digest=token_digest,
                    actor=request.user,
                )
                .first()
            )
            if confirmation is None:
                return JsonResponse(
                    {"error": "A confirmação não é válida, expirou ou já foi usada."},
                    status=409,
                )
            if confirmation.expires_at <= timezone.now():
                confirmation.delete()
                return JsonResponse(
                    {"error": "A confirmação expirou. Gere uma nova prévia."},
                    status=410,
                )
            if (
                confirmation.action
                == MaintenanceConfirmation.Action.RESET_ELECTION
                and payload.get("confirmation_phrase")
                != ELECTION_RESET_CONFIRMATION
            ):
                return JsonResponse(
                    {"error": "Digite exatamente REINICIAR ELEIÇÃO para confirmar."},
                    status=400,
                )

            try:
                plan = _build_maintenance_plan(
                    confirmation.action,
                    confirmation.target_id,
                    request.user,
                )
            except (User.DoesNotExist, ShirtProposal.DoesNotExist):
                confirmation.delete()
                return JsonResponse(
                    {"error": "A prévia ficou desatualizada. Gere uma nova."},
                    status=409,
                )
            except ValidationError:
                confirmation.delete()
                return JsonResponse(
                    {"error": "A operação não é mais permitida. Gere uma nova prévia."},
                    status=409,
                )

            if (
                plan["snapshot_digest"] != confirmation.snapshot_digest
                or plan["audit_counts"] != confirmation.summary
            ):
                confirmation.delete()
                return JsonResponse(
                    {"error": "Os dados mudaram desde a prévia. Gere uma nova."},
                    status=409,
                )

            _apply_maintenance_action(
                confirmation.action,
                confirmation.target_id,
                plan,
                request.user,
            )
            completed_action = confirmation.action
            affected_counts = plan["audit_counts"]
            warning = plan["warning"]
            MaintenanceAudit.objects.create(
                actor=request.user,
                actor_identifier=request.user.pk,
                action=completed_action,
                affected_counts=affected_counts,
            )
            confirmation.delete()
    except ValidationError as exc:
        return _validation_error_response(exc)
    except DatabaseError:
        logger.exception("Falha ao executar uma operação de manutenção.")
        return JsonResponse(
            {"error": "Não foi possível concluir a operação de manutenção."},
            status=500,
        )

    cleanup_failures = _process_maintenance_file_cleanup()
    message = {
        MaintenanceConfirmation.Action.DELETE_USER:
            "Conta e registros relacionados excluídos.",
        MaintenanceConfirmation.Action.DELETE_PROPOSAL:
            "Proposta e votos relacionados excluídos.",
        MaintenanceConfirmation.Action.RESET_ELECTION:
            "Dados eleitorais reiniciados; contas e perfis preservados.",
    }[completed_action]
    return JsonResponse({
        "message": message,
        "affected": affected_counts,
        "warning": warning,
        "file_cleanup_pending": cleanup_failures > 0,
    })


@require_GET
@never_cache
def admin_progress(request):
    try:
        _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    settings = ElectionSettings.objects.filter(pk=1).first()
    active_students = User.objects.filter(
        is_active=True,
        profile__role__in=UserProfile.STUDENT_ROLES,
    ).count()
    preferences_count = PreferenceResponse.objects.filter(
        student__is_active=True,
        student__profile__role__in=UserProfile.STUDENT_ROLES,
    ).values("student_id").distinct().count()
    proposal_counts = {
        status: ShirtProposal.objects.filter(status=status).count()
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.APPROVED,
            ShirtProposal.Status.REJECTED,
        )
    }
    return JsonResponse({
        "students_total": active_students,
        "students_active": active_students,
        "survey_responses": preferences_count,
        "survey_participation_percentage": (
            round((preferences_count / active_students) * 100, 2)
            if active_students
            else None
        ),
        "proposal_counts": proposal_counts,
        "voting_open": bool(settings and settings.voting_open),
        "total_votes": Vote.objects.count(),
    })


@require_GET
@never_cache
def student_dashboard_status(request):
    if not request.user.is_authenticated:
        return JsonResponse(
            {"error": "Autenticação necessária."},
            status=401,
        )

    try:
        is_student = _is_authorized_student(request.user)
        is_admin = _is_active_admin_user(request.user)
        if not is_student and not is_admin:
            raise PermissionDenied("Acesso permitido apenas a estudantes ativos.")
        preferences = (
            get_preference_response(request.user)
            if is_student
            else None
        )
        settings = ElectionSettings.objects.filter(pk=1).first()
        response = {
            "survey_open": is_survey_open(),
            "proposal_submission_open": (
                settings is None or settings.proposal_submission_open is not False
            ),
            "voting_open": settings is not None and settings.voting_open,
            "results_release_authorized": is_results_release_authorized(),
            "has_preferences": preferences is not None,
            "has_voted": (
                Vote.objects.filter(student=request.user).exists()
                if is_student
                else False
            ),
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
        proposals = list(
            ShirtProposal.objects.filter(
                status=ShirtProposal.Status.APPROVED,
            ).order_by("-created_at", "-pk")
            if _is_active_admin_user(request.user)
            else get_approved_proposals(request.user)
        )
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
        settings = ElectionSettings.objects.filter(pk=1).first()
        if request.method == "POST":
            if settings is not None and settings.proposal_submission_open is False:
                return JsonResponse(
                    {"error": "O envio de propostas está fechado."},
                    status=403,
                )
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
        return user.profile.role in UserProfile.STUDENT_ROLES
    except UserProfile.DoesNotExist:
        return False


def _is_authorized_admin(request):
    if (
        not getattr(request.user, "is_authenticated", False)
        or not getattr(request.user, "is_active", False)
    ):
        return False

    try:
        return request.user.profile.role in UserProfile.ADMIN_ROLES
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
