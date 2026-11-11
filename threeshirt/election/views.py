import json
import hashlib
import hmac
import io
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, nullcontext
from datetime import timedelta
from pathlib import Path

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, IntegrityError, connection, transaction
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
    AccountAdminAudit,
    PasswordAccessRequest,
    UserProfile,
)
from accounts.account_sessions import revoke_user_sessions

from .models import (
    ElectionSettings,
    ElectionResultSnapshot,
    MaintenanceAudit,
    MaintenanceConfirmation,
    MaintenanceFileCleanup,
    ParticipationAuditEvent,
    ParticipationIdentity,
    PreferenceResponse,
    ShirtProposal,
    Vote,
    VoteReplacementAuthorization,
    get_or_create_current_identity,
)
from .services import (
    get_approved_proposals,
    get_compatibility_tally,
    get_result_snapshot,
    get_preference_tally,
    get_preference_response,
    get_vote_tally,
    is_results_release_authorized,
    is_survey_open,
    create_result_snapshot,
    cast_vote,
    save_preference_response,
    submit_shirt_proposal,
)
from .storage import PrivateProposalStorage
from .validators import get_proposal_image_format

logger = logging.getLogger(__name__)

MAINTENANCE_CONFIRMATION_TTL = timedelta(minutes=10)
MAINTENANCE_ACTIONS = {
    "reset_user_basic": MaintenanceConfirmation.Action.RESET_USER_BASIC,
    "reset_user_full": MaintenanceConfirmation.Action.RESET_USER_FULL,
    "delete_user": MaintenanceConfirmation.Action.DELETE_USER,
    "delete_proposal": MaintenanceConfirmation.Action.DELETE_PROPOSAL,
    "reset_election": MaintenanceConfirmation.Action.RESET_ELECTION,
}
ELECTION_RESET_CONFIRMATION = "REINICIAR ELEIÇÃO"


class MaintenanceBackupError(Exception):
    pass


def _create_verified_sqlite_backup(backup_dir, stamp):
    database_name = connection.settings_dict.get("NAME")
    if not database_name:
        raise MaintenanceBackupError(
            "Não foi possível identificar um arquivo SQLite para o backup."
        )
    is_memory_database = (
        str(database_name) == ":memory:"
        or str(database_name).startswith("file:")
    )
    active_path = None if is_memory_database else Path(database_name).resolve()
    if active_path is not None and not active_path.is_file():
        raise MaintenanceBackupError(
            "O arquivo do banco SQLite ativo não está acessível."
        )

    filename = f"threeshirt-maintenance-{stamp}-{secrets.token_hex(4)}.sqlite3"
    backup_path = backup_dir / filename
    temporary_path = backup_dir / f".{filename}.tmp"
    try:
        if is_memory_database:
            connection.ensure_connection()
            source = connection.connection
            source_context = nullcontext(source)
        else:
            source_context = closing(
                sqlite3.connect(active_path, timeout=30)
            )
        with source_context as source:
            with closing(sqlite3.connect(temporary_path, timeout=30)) as destination:
                source.backup(destination)
                integrity = destination.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise MaintenanceBackupError(
                        "A cópia do banco não passou na verificação de integridade."
                    )

        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise MaintenanceBackupError(
                "O arquivo de backup está vazio ou inacessível."
            )
        temporary_path.rename(backup_path)

        with closing(sqlite3.connect(backup_path, timeout=30)) as verified:
            integrity = verified.execute(
                "PRAGMA integrity_check"
            ).fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MaintenanceBackupError(
                "O backup final não passou na verificação de integridade."
            )
        return backup_path
    except MaintenanceBackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        logger.exception("Falha ao criar ou verificar backup de manutenção.")
        raise MaintenanceBackupError(
            "Não foi possível criar e verificar o backup do banco."
        ) from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _postgres_environment():
    database = connection.settings_dict
    inherited_environment = os.environ
    environment = {
        name: inherited_environment[name]
        for name in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
        )
        if name in inherited_environment
    }
    for setting_name, variable_name in (
        ("HOST", "PGHOST"),
        ("PORT", "PGPORT"),
        ("USER", "PGUSER"),
        ("PASSWORD", "PGPASSWORD"),
        ("NAME", "PGDATABASE"),
    ):
        value = database.get(setting_name)
        if value not in (None, ""):
            environment[variable_name] = str(value)

    options = database.get("OPTIONS", {})
    for option_name, variable_name in (
        ("sslmode", "PGSSLMODE"),
        ("sslrootcert", "PGSSLROOTCERT"),
        ("sslcert", "PGSSLCERT"),
        ("sslkey", "PGSSLKEY"),
        ("connect_timeout", "PGCONNECT_TIMEOUT"),
    ):
        value = options.get(option_name)
        if value not in (None, ""):
            environment[variable_name] = str(value)
    return environment


def _create_verified_postgresql_backup(backup_dir, stamp):
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        raise MaintenanceBackupError(
            "As ferramentas pg_dump e pg_restore precisam estar instaladas."
        )

    database = connection.settings_dict
    database_name = database.get("NAME")
    if not database_name:
        raise MaintenanceBackupError(
            "Não foi possível identificar o banco PostgreSQL para o backup."
        )

    filename = f"threeshirt-maintenance-{stamp}-{secrets.token_hex(4)}.dump"
    backup_path = backup_dir / filename
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.",
        suffix=".tmp",
        dir=backup_dir,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        environment = _postgres_environment()
        command = [
            pg_dump,
            "--format=custom",
            "--no-password",
            "--file",
            str(temporary_path),
        ]
        if database.get("HOST"):
            command.extend(["--host", str(database["HOST"])])
        if database.get("PORT"):
            command.extend(["--port", str(database["PORT"])])
        if database.get("USER"):
            command.extend(["--username", str(database["USER"])])
        command.extend(["--dbname", str(database_name)])

        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=600,
            env=environment,
        )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise MaintenanceBackupError(
                "O backup PostgreSQL está vazio ou inacessível."
            )
        subprocess.run(
            [
                pg_restore,
                "--list",
                "--no-password",
                str(temporary_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            env=environment,
        )
        temporary_path.replace(backup_path)
        return backup_path
    except MaintenanceBackupError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception("Falha ao criar ou verificar backup PostgreSQL.")
        raise MaintenanceBackupError(
            "Não foi possível criar e verificar o backup PostgreSQL."
        ) from exc
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _backup_digest(backup_path):
    digest = hashlib.sha256()
    with backup_path.open("rb") as backup_file:
        for chunk in iter(lambda: backup_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_verified_database_backup():
    backup_dir = Path(
        getattr(
            settings,
            "MAINTENANCE_BACKUP_DIR",
            Path(settings.BASE_DIR) / "backups" / "maintenance",
        )
    ).resolve()
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MaintenanceBackupError(
            "Não foi possível preparar o diretório de backup."
        ) from exc

    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S-%f")
    if connection.vendor == "sqlite":
        backup_path = _create_verified_sqlite_backup(backup_dir, stamp)
    elif connection.vendor == "postgresql":
        backup_path = _create_verified_postgresql_backup(backup_dir, stamp)
    else:
        raise MaintenanceBackupError(
            "O backup de manutenção não suporta este banco de dados."
        )

    return {
        "path": str(backup_path),
        "sha256": _backup_digest(backup_path),
        "integrity": "ok",
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
    identity = ParticipationIdentity.objects.filter(
        user=user,
        is_current=True,
    ).first()
    has_preferences = bool(
        identity
        and PreferenceResponse.objects.filter(
            participation_identity=identity,
            is_invalidated=False,
        ).exists()
    )
    has_voted = bool(
        identity
        and Vote.objects.filter(
            participation_identity=identity,
            is_invalidated=False,
        ).exists()
    )
    has_proposals = bool(
        identity
        and ShirtProposal.objects.filter(author_identity=identity).exists()
    )
    return {
        "id": user.pk,
        "email": user.email,
        "full_name": (profile.full_name if profile else ""),
        "role": profile.role if profile else _role_for_user(user),
        "is_active": bool(user.is_active),
        "is_staff": bool(user.is_staff),
        "has_password": user.has_usable_password(),
        "has_participation": has_preferences or has_voted or has_proposals,
        "current_generation": identity.generation if identity else 0,
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


def _role_for_user(user):
    try:
        return user.profile.role
    except UserProfile.DoesNotExist:
        email = getattr(user, "email", "").strip().casefold()
        admin_emails = {
            value.strip().casefold()
            for value in getattr(settings, "ADMIN_EMAILS", [])
            if isinstance(value, str) and value.strip()
        }
        return (
            UserProfile.Role.ADMIN
            if email and email in admin_emails
            else UserProfile.Role.STUDENT
        )


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


def _build_account_reset_plan(target_id, actor, action):
    user = User.objects.select_related("profile").filter(pk=target_id).first()
    if user is None:
        raise User.DoesNotExist
    if user.pk == actor.pk:
        raise ValidationError("Não é permitido reinicializar a própria conta.")
    role = _role_for_user(user)
    if (
        getattr(getattr(actor, "profile", None), "role", None)
        == UserProfile.Role.STUDENT_ADMIN
        and role == UserProfile.Role.ADMIN
    ):
        raise PermissionDenied(
            "Estudantes-administradores não podem reinicializar contas ADMIN."
        )
    if (
        action == MaintenanceConfirmation.Action.RESET_USER_FULL
        and _is_active_maintenance_admin(actor) is False
    ):
        raise PermissionDenied("A reinicialização completa exige papel ADMIN.")
    if (
        role == UserProfile.Role.ADMIN
        and user.is_active
        and User.objects.filter(
            is_active=True,
            profile__role=UserProfile.Role.ADMIN,
        ).exclude(pk=user.pk).count() == 0
    ):
        raise ValidationError(
            "Não é possível remover o último administrador ativo."
        )
    try:
        identity = ParticipationIdentity.objects.get(
            user=user,
            is_current=True,
        )
    except ParticipationIdentity.DoesNotExist:
        identity = None
    response_ids = (
        list(
            PreferenceResponse.objects.filter(
                participation_identity=identity
            ).values_list("pk", flat=True)
        )
        if identity
        else list(
            PreferenceResponse.objects.filter(student_id=user.pk)
            .values_list("pk", flat=True)
        )
    )
    vote_ids = (
        list(
            Vote.objects.filter(
                participation_identity=identity
            ).values_list("pk", flat=True)
        )
        if identity
        else list(
            Vote.objects.filter(student_id=user.pk)
            .values_list("pk", flat=True)
        )
    )
    proposals = list(
        ShirtProposal.objects.filter(
            Q(author_identity=identity) if identity else Q(author_id=user.pk)
        ).values("pk", "image", "status")
    )
    proposal_ids = [proposal["pk"] for proposal in proposals]
    other_votes = Vote.objects.filter(
        proposal_id__in=proposal_ids,
    )
    if identity:
        other_votes = other_votes.exclude(participation_identity=identity)
    else:
        other_votes = other_votes.exclude(student_id=user.pk)
    other_vote_ids = list(other_votes.values_list("pk", flat=True))
    preserved_proposal_ids = set(
        other_votes.values_list("proposal_id", flat=True)
    )
    removed_proposal_ids = (
        set(proposal_ids) - preserved_proposal_ids
        if action == MaintenanceConfirmation.Action.RESET_USER_FULL
        else set()
    )
    target_image_names = {
        proposal["image"]
        for proposal in proposals
        if proposal["image"]
    }
    remaining_image_names = set(
        ShirtProposal.objects.exclude(pk__in=removed_proposal_ids)
        .filter(image__in=target_image_names)
        .values_list("image", flat=True)
    )
    summary = {
        "account_preserved": 1,
        "email_preserved": 1,
        "profiles_reset": int(hasattr(user, "profile")),
        "responses_preserved": len(response_ids)
        if action == MaintenanceConfirmation.Action.RESET_USER_BASIC
        else 0,
        "responses_removed": len(response_ids)
        if action == MaintenanceConfirmation.Action.RESET_USER_FULL
        else 0,
        "votes_preserved": len(vote_ids)
        if action == MaintenanceConfirmation.Action.RESET_USER_BASIC
        else 0,
        "votes_removed": len(vote_ids)
        if action == MaintenanceConfirmation.Action.RESET_USER_FULL
        else 0,
        "proposals_preserved": (
            len(proposals)
            if action == MaintenanceConfirmation.Action.RESET_USER_BASIC
            else len(preserved_proposal_ids)
        ),
        "proposals_removed": (
            0
            if action == MaintenanceConfirmation.Action.RESET_USER_BASIC
            else len(removed_proposal_ids)
        ),
        "third_party_votes_preserved": len(other_vote_ids),
        "proposal_images_preserved": len(remaining_image_names),
    }
    snapshot = {
        "user_id": user.pk,
        "role": role,
        "active": user.is_active,
        "identity_id": identity.pk if identity else None,
        "responses": response_ids,
        "votes": vote_ids,
        "proposals": proposals,
        "other_vote_ids": other_vote_ids,
        "preserved_proposal_ids": sorted(preserved_proposal_ids),
        "removed_proposal_ids": sorted(removed_proposal_ids),
        "remaining_image_names": sorted(remaining_image_names),
        "action": action,
    }
    profile = getattr(user, "profile", None)
    target = {
        "id": user.pk,
        "label": (profile.full_name if profile else "") or user.email,
        "email": user.email,
        "role": role or UserProfile.Role.STUDENT,
    }
    return {
        "target_id": user.pk,
        "target": target,
        "audit_target_identifier": user.pk,
        "summary": summary,
        "audit_counts": summary,
        "snapshot_digest": _snapshot_digest(snapshot),
        "warning": (
            "A reinicialização completa removerá apenas participações desta "
            "geração. Propostas com votos de terceiros, seus votos e imagens "
            "serão preservados."
            if action == MaintenanceConfirmation.Action.RESET_USER_FULL
            else "A conta e o e-mail serão mantidos; o histórico eleitoral "
            "será preservado e não será associado ao próximo titular."
        ),
        "files": [],
    }


def _check_account_deletion_allowed(user, actor):
    role = _role_for_user(user)

    actor_role = getattr(
        getattr(actor, "profile", None),
        "role",
        None,
    )
    if actor_role == UserProfile.Role.STUDENT_ADMIN and role == UserProfile.Role.ADMIN:
        raise PermissionDenied(
            "Estudantes-administradores não podem excluir contas ADMIN."
        )

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
    user = User.objects.select_related("profile").filter(pk=target_id).first()
    if user is None:
        raise User.DoesNotExist

    _check_account_deletion_allowed(user, actor)
    identity_ids = list(
        ParticipationIdentity.objects.filter(user_id=user.pk)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    profiles = list(
        UserProfile.objects.filter(user_id=user.pk)
        .values("pk", "role", "full_name")
    )
    preferences = list(
        PreferenceResponse.objects.filter(
            Q(student_id=user.pk)
            | Q(participation_identity_id__in=identity_ids)
        )
        .values_list("pk", flat=True)
    )
    proposals = list(
        ShirtProposal.objects.filter(
            Q(author_id=user.pk)
            | Q(author_identity_id__in=identity_ids)
        )
        .values("pk", "status", "image", "reviewed_by_id")
    )
    proposal_ids = [proposal["pk"] for proposal in proposals]
    votes = list(
        Vote.objects.filter(
            Q(student_id=user.pk)
            | Q(participation_identity_id__in=identity_ids)
            | Q(proposal_id__in=proposal_ids)
        )
        .order_by("pk")
        .values("pk", "student_id", "participation_identity_id", "proposal_id")
    )
    participation_audit_ids = list(
        ParticipationAuditEvent.objects.filter(
            participation_identity_id__in=identity_ids
        ).values_list("pk", flat=True)
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
    account_audit_count = AccountAdminAudit.objects.filter(
        target_identifier=user.pk
    ).count()
    password_request_count = PasswordAccessRequest.objects.filter(
        user_id=user.pk
    ).count()
    replacement_authorization_count = VoteReplacementAuthorization.objects.filter(
        participation_identity_id__in=identity_ids
    ).count()
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
        "votes_from_other_participants": sum(
            vote["student_id"] != user.pk
            and vote["participation_identity_id"] not in identity_ids
            for vote in votes
        ),
        "group_memberships": len(group_ids),
        "direct_permissions": len(permission_ids),
        "admin_log_entries": len(admin_log_ids),
        "proposal_images": len(image_names),
        "files_to_remove": len(files_to_remove),
        "participation_identities_preserved": len(identity_ids),
        "participation_audit_events_preserved": len(participation_audit_ids),
        "account_audit_events_preserved": account_audit_count,
        "password_requests_preserved": password_request_count,
        "replacement_authorizations_preserved": replacement_authorization_count,
        "admin_log_entries_preserved": len(admin_log_ids),
        "result_snapshots_preserved": ElectionResultSnapshot.objects.count(),
    }
    snapshot = {
        "user": {
            "pk": user.pk,
            "is_active": user.is_active,
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
            "last_login": _timestamp_snapshot(user.last_login),
        },
        "profiles": profiles,
        "identities": identity_ids,
        "preferences": preferences,
        "proposals": proposals,
        "votes": votes,
        "participation_audit_ids": participation_audit_ids,
        "groups": group_ids,
        "permissions": permission_ids,
        "admin_logs": admin_log_ids,
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
    warnings.append(
        "Contas, perfis, propostas relacionadas, respostas e votos serão "
        "removidos conforme a prévia. Identidades, eventos de participação, "
        "auditorias administrativas, registros de Django, credenciais de "
        "primeiro acesso, solicitações de senha e snapshots serão preservados."
    )
    if remaining_image_refs:
        warnings.append(
            "Imagens ainda referenciadas por outras propostas serão preservadas."
        )
    role = profiles[0]["role"] if profiles else UserProfile.Role.STUDENT
    if (
        getattr(getattr(actor, "profile", None), "role", None)
        == UserProfile.Role.STUDENT_ADMIN
        and role == UserProfile.Role.ADMIN
    ):
        raise PermissionDenied(
            "Estudantes-administradores não podem excluir contas ADMIN."
        )
    return {
        "target_id": user.pk,
        "target": target,
        "audit_target_identifier": user.pk,
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
        .values("pk", "student_id", "participation_identity_id")
    )
    participation_audit_count = ParticipationAuditEvent.objects.filter(
        participation_type=ParticipationAuditEvent.ParticipationType.PROPOSAL,
        participation_id=proposal.pk,
    ).count()
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
        "votes_from_other_participants": sum(
            vote["student_id"] != proposal.author_id for vote in votes
        ),
        "proposal_images": len(image_names),
        "files_to_remove": len(files_to_remove),
        "participation_audit_events_preserved": participation_audit_count,
        "result_snapshots_preserved": ElectionResultSnapshot.objects.count(),
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
            "associados serão excluídos. Snapshots publicados e eventos de "
            "auditoria serão preservados."
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
        "audit_target_identifier": proposal.pk,
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
        .values("pk", "student_id", "participation_identity_id", "proposal_id")
    )
    pending_authorizations = list(
        VoteReplacementAuthorization.objects.filter(
            consumed_at__isnull=True
        ).values("pk", "participation_identity_id", "actor_identifier", "reason")
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
        "participation_identities_preserved": ParticipationIdentity.objects.count(),
        "participation_audit_events_preserved": ParticipationAuditEvent.objects.count(),
        "account_audit_events_preserved": AccountAdminAudit.objects.count(),
        "result_snapshots_preserved": ElectionResultSnapshot.objects.count(),
        "maintenance_audit_events_preserved": MaintenanceAudit.objects.count(),
        "pending_replacement_authorizations_removed": len(pending_authorizations),
    }
    snapshot = {
        "proposals": proposals,
        "preferences": preferences,
        "votes": votes,
        "pending_authorizations": pending_authorizations,
        "settings": settings_snapshot,
        "preserved": {
            "accounts": summary["accounts_preserved"],
            "profiles": summary["profiles_preserved"],
            "participation_identities": summary[
                "participation_identities_preserved"
            ],
            "participation_audit_events": summary[
                "participation_audit_events_preserved"
            ],
            "account_audit_events": summary["account_audit_events_preserved"],
            "result_snapshots": summary["result_snapshots_preserved"],
            "maintenance_audit_events": summary[
                "maintenance_audit_events_preserved"
            ],
        },
    }
    return {
        "target_id": None,
        "target": {"label": "Todos os dados eleitorais"},
        "audit_target_identifier": None,
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
            "Serão removidos propostas, votos, respostas e autorizações "
            "pendentes de substituição de voto. Contas, perfis, identidades "
            "de participação, eventos de auditoria, snapshots publicados e "
            "histórico de manutenção serão preservados. As configurações "
            "operacionais voltarão ao estado inicial."
        ),
    }


def _build_maintenance_plan(action, target_id, actor):
    if action == MaintenanceConfirmation.Action.DELETE_USER:
        return _build_user_deletion_plan(target_id, actor)
    if action == MaintenanceConfirmation.Action.DELETE_PROPOSAL:
        if not _is_active_maintenance_admin(actor):
            raise PermissionDenied(
                "A exclusão de propostas exige papel ADMIN."
            )
        return _build_proposal_deletion_plan(target_id)
    if action == MaintenanceConfirmation.Action.RESET_ELECTION:
        if not _is_active_maintenance_admin(actor):
            raise PermissionDenied(
                "A reinicialização eleitoral exige papel ADMIN."
            )
        return _build_election_reset_plan()
    if action in (
        MaintenanceConfirmation.Action.RESET_USER_BASIC,
        MaintenanceConfirmation.Action.RESET_USER_FULL,
    ):
        return _build_account_reset_plan(target_id, actor, action)
    raise ValidationError("Ação de manutenção não disponível.")


def _apply_maintenance_action(action, target_id, plan, actor, reason):
    if action in (
        MaintenanceConfirmation.Action.DELETE_USER,
        MaintenanceConfirmation.Action.DELETE_PROPOSAL,
        MaintenanceConfirmation.Action.RESET_ELECTION,
    ):
        for _storage, name in plan["files"]:
            MaintenanceFileCleanup.objects.get_or_create(storage_name=name)

        if action == MaintenanceConfirmation.Action.DELETE_USER:
            user = User.objects.select_for_update().get(pk=target_id)
            _check_account_deletion_allowed(user, actor)
            identities = ParticipationIdentity.objects.filter(user=user)
            identity_ids = list(identities.values_list("pk", flat=True))
            proposal_ids = list(
                ShirtProposal.objects.filter(
                    Q(author_id=user.pk)
                    | Q(author_identity_id__in=identity_ids)
                ).values_list("pk", flat=True)
            )
            Vote.objects.filter(
                Q(student_id=user.pk)
                | Q(participation_identity_id__in=identity_ids)
                | Q(proposal_id__in=proposal_ids)
            ).delete()
            PreferenceResponse.objects.filter(
                Q(student_id=user.pk)
                | Q(participation_identity_id__in=identity_ids)
            ).delete()
            ShirtProposal.objects.filter(pk__in=proposal_ids).delete()
            LogEntry.objects.filter(user_id=user.pk).update(user=None)
            revoke_user_sessions(user.pk)
            user.delete()
            return

        if action == MaintenanceConfirmation.Action.DELETE_PROPOSAL:
            proposal = ShirtProposal.objects.select_for_update().get(pk=target_id)
            Vote.objects.filter(proposal=proposal).delete()
            proposal.delete()
            return

        Vote.objects.all().delete()
        PreferenceResponse.objects.all().delete()
        ShirtProposal.objects.all().delete()
        VoteReplacementAuthorization.objects.filter(
            consumed_at__isnull=True
        ).delete()
        election_settings = ElectionSettings.objects.filter(pk=1).first()
        if election_settings is None:
            election_settings = ElectionSettings(pk=1)
        election_settings.survey_open = False
        election_settings.proposal_submission_open = True
        election_settings.voting_open = False
        election_settings.results_release_authorized = False
        election_settings.survey_closed_at = None
        election_settings.voting_started_at = None
        election_settings.voting_ended_at = None
        election_settings.compatibility_weight = 50
        election_settings.full_clean()
        election_settings.save()
        return

    if action not in (
        MaintenanceConfirmation.Action.RESET_USER_BASIC,
        MaintenanceConfirmation.Action.RESET_USER_FULL,
    ):
        raise ValidationError("Ação de manutenção não disponível.")
    if action == MaintenanceConfirmation.Action.RESET_USER_FULL and not (
        _is_active_maintenance_admin(actor)
    ):
        raise PermissionDenied("A reinicialização completa exige papel ADMIN.")

    user = User.objects.select_for_update().get(pk=target_id)
    _check_account_deletion_allowed(user, actor)
    profile, _created = UserProfile.objects.get_or_create(user=user)
    identity = (
        ParticipationIdentity.objects.select_for_update()
        .filter(user=user, is_current=True)
        .first()
    )
    if identity is None:
        identity = get_or_create_current_identity(user)

    if (
        action == MaintenanceConfirmation.Action.RESET_USER_FULL
        and is_results_release_authorized()
    ):
        create_result_snapshot(actor, "account_full_reset_before")

    responses = PreferenceResponse.objects.filter(
        participation_identity=identity,
    )
    votes = Vote.objects.filter(participation_identity=identity)
    proposals = ShirtProposal.objects.filter(author_identity=identity)

    if action == MaintenanceConfirmation.Action.RESET_USER_FULL:
        proposal_ids = list(proposals.values_list("pk", flat=True))
        external_votes = Vote.objects.filter(
            proposal_id__in=proposal_ids,
        ).exclude(participation_identity=identity)
        preserved_proposal_ids = set(
            external_votes.values_list("proposal_id", flat=True)
        )
        image_names = list(
            proposals.exclude(pk__in=preserved_proposal_ids)
            .exclude(image="")
            .values_list("image", flat=True)
        )
        responses.delete()
        votes.delete()
        proposals.exclude(pk__in=preserved_proposal_ids).delete()
        if image_names:
            storage = ShirtProposal._meta.get_field("image").storage
            for name in image_names:
                if not ShirtProposal.objects.filter(image=name).exists():
                    MaintenanceFileCleanup.objects.get_or_create(
                        storage_name=name
                    )

    identity.is_current = False
    identity.user = None
    identity.ended_at = timezone.now()
    identity.display_label = f"Participante histórico {identity.pk}"
    identity.save(
        update_fields=["is_current", "user", "ended_at", "display_label"]
    )
    PreferenceResponse.objects.filter(
        participation_identity=identity
    ).update(student=None)
    Vote.objects.filter(participation_identity=identity).update(student=None)
    ShirtProposal.objects.filter(
        author_identity=identity
    ).update(author=None)

    next_generation = identity.generation + 1
    ParticipationIdentity.objects.create(
        user=user,
        original_user_id=user.pk,
        generation=next_generation,
        display_label=f"Participante {user.pk}-{next_generation}",
        is_current=True,
        is_eligible=True,
    )

    user.set_unusable_password()
    user.is_active = True
    user.is_staff = False
    user.is_superuser = False
    user.first_name = ""
    user.last_name = ""
    user.last_login = None
    user.save(
        update_fields=[
            "password",
            "is_active",
            "is_staff",
            "is_superuser",
            "first_name",
            "last_name",
            "last_login",
        ]
    )
    profile.full_name = ""
    profile.role = UserProfile.Role.STUDENT
    profile.credential_revision += 1
    profile.save(
        update_fields=["full_name", "role", "credential_revision"]
    )
    user.groups.clear()
    user.user_permissions.clear()
    revoke_user_sessions(user.pk)
    PasswordAccessRequest.objects.filter(
        user=user,
        status__in=[
            PasswordAccessRequest.Status.PENDING,
            PasswordAccessRequest.Status.APPROVED,
        ],
    ).update(
        status=PasswordAccessRequest.Status.EXPIRED,
        closed_reason="ACCOUNT_RESET",
    )
    AccountAdminAudit.objects.create(
        actor=actor,
        actor_identifier=actor.pk,
        target=user,
        target_identifier=user.pk,
        action=AccountAdminAudit.Action.ACCOUNT_RESET,
        reason=reason,
        details={
            "mode": action,
            "participation_identity": identity.pk,
            "summary": plan["summary"],
        },
    )
    if (
        action == MaintenanceConfirmation.Action.RESET_USER_FULL
        and is_results_release_authorized()
    ):
        create_result_snapshot(actor, "account_full_reset_after")


def _process_maintenance_file_cleanup():
    storage = ShirtProposal._meta.get_field("image").storage
    pending = list(MaintenanceFileCleanup.objects.order_by("pk"))
    failures = 0
    for item in pending:
        if ShirtProposal.objects.filter(image=item.storage_name).exists():
            try:
                item.delete()
            except DatabaseError:
                failures += 1
                logger.exception(
                    "Falha ao remover uma limpeza desnecessária de imagem."
                )
            continue
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
    eligible_participants = ParticipationIdentity.objects.filter(
        is_eligible=True
    ).count()
    total_active_users = User.objects.filter(is_active=True).count()
    responses = PreferenceResponse.objects.filter(
        is_invalidated=False,
        participation_identity__is_eligible=True,
    ).count()
    proposal_counts = {
        status: ShirtProposal.objects.filter(status=status).count()
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.APPROVED,
            ShirtProposal.Status.REJECTED,
        )
    }
    vote_tally = get_vote_tally()

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
            "votes_cast": vote_tally["total_votes"],
            "admins_total": User.objects.filter(
                is_active=True,
                profile__role__in=UserProfile.ADMIN_ROLES,
            ).count(),
        },
        "progress": {
            "survey_participation": (
                round((responses / eligible_participants) * 100, 2)
                if eligible_participants
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
        "author_identity",
        "reviewed_by",
        "reviewed_by__profile",
    ).order_by("-created_at")
    payload = []
    for proposal in proposals:
        preferences = (
            PreferenceResponse.objects.filter(
                participation_identity=proposal.author_identity,
                is_invalidated=False,
            ).first()
            if proposal.author_identity_id
            else None
        )

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
            "is_invalidated": proposal.is_invalidated,
            "image_url": reverse("proposal-image", args=[proposal.pk]),
            "preferences": (
                {
                    "base_color": preferences.base_color,
                    "related_to_course": preferences.related_to_course,
                    "wants_animal": preferences.wants_animal,
                    "preferred_animal": preferences.preferred_animal,
                    "preferred_colors": preferences.preferred_colors,
                    "wants_collar": preferences.wants_collar,
                    "wants_phrase": preferences.wants_phrase,
                    "intends_to_buy": preferences.intends_to_buy,
                }
                if preferences is not None
                else None
            ),
            "author": {
                "id": (
                    proposal.author_identity.original_user_id
                    if proposal.author_identity_id
                    else 0
                ),
                "email": "",
                "full_name": (
                    proposal.author_identity.display_label
                    if proposal.author_identity_id
                    else "Participante histórico"
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
            results_were_released = bool(
                settings
                and settings.results_release_authorized
                and not settings.voting_open
            )
            if results_were_released:
                create_result_snapshot(
                    request.user,
                    "proposal_review_before",
                )
            try:
                proposal.full_clean()
            finally:
                proposal.image.close()
            proposal.save()
            if results_were_released:
                create_result_snapshot(
                    request.user,
                    "proposal_review_after",
                )
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
                    if next_value and settings.voting_open:
                        raise ValidationError({
                            "results_release": (
                                "Os resultados só podem ser liberados após o encerramento."
                            )
                        })
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
                if stage == "results_release" and settings.results_release_authorized:
                    create_result_snapshot(request.user, "results_release")
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
        actor = _require_admin_user(request)
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a administradores ativos."},
            status=403,
        )

    users = User.objects.select_related("profile").order_by("email", "pk")
    if actor.profile.role == UserProfile.Role.STUDENT_ADMIN:
        users = users.filter(
            profile__role__in=(
                UserProfile.Role.STUDENT,
                UserProfile.Role.STUDENT_ADMIN,
            )
        )
    return JsonResponse({
        "users": [_serialize_user_summary(user) for user in users]
    })


@require_http_methods(["POST"])
@never_cache
@transaction.atomic
def admin_user_permission(request, user_id):
    try:
        actor = _require_admin_user(request)
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
    if not isinstance(payload, dict):
        return JsonResponse({"error": "Informe um objeto JSON."}, status=400)

    try:
        with transaction.atomic():
            user = (
                User.objects.select_for_update()
                .select_related("profile")
                .get(pk=user_id)
            )
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

    current_role = _role_for_user(user)
    if actor.profile.role == UserProfile.Role.STUDENT_ADMIN and (
        current_role == UserProfile.Role.ADMIN
        or target_role == UserProfile.Role.ADMIN
    ):
        return JsonResponse(
            {"error": "Estudantes-administradores não podem modificar contas ADMIN nem promover usuários."},
            status=403,
        )
    profile, profile_created = UserProfile.objects.get_or_create(
        user=user,
        defaults={"role": current_role},
    )
    user.profile = profile
    if (
        user.is_active
        and current_role == UserProfile.Role.ADMIN
        and target_role != UserProfile.Role.ADMIN
    ):
        remaining_admins = User.objects.filter(
            is_active=True,
            profile__role=UserProfile.Role.ADMIN,
        ).exclude(pk=user.pk).count()
        if remaining_admins == 0:
            return JsonResponse(
                {"error": "O sistema precisa manter pelo menos um ADMIN ativo."},
                status=400,
            )

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        return JsonResponse(
            {"error": "Informe uma justificativa obrigatória."},
            status=400,
        )
    previous_role = profile.role
    profile.role = target_role
    role_changed = profile_created or previous_role != target_role
    if role_changed:
        profile.credential_revision += 1
    profile.save(update_fields=["role", "credential_revision"])
    user.is_superuser = False
    user.is_staff = (
        target_role == UserProfile.Role.ADMIN
        and user.email.strip().casefold()
        in {
            email.strip().casefold()
            for email in getattr(settings, "ADMIN_EMAILS", [])
            if isinstance(email, str) and email.strip()
        }
    )
    user.save(update_fields=["is_staff", "is_superuser"])
    if role_changed:
        revoke_user_sessions(user.pk)
    AccountAdminAudit.objects.create(
        actor=request.user,
        actor_identifier=request.user.pk,
        target=user,
        target_identifier=user.pk,
        action=AccountAdminAudit.Action.ROLE_CHANGED,
        reason=reason.strip(),
        details={"from": previous_role, "to": target_role},
    )
    return JsonResponse({
        "message": "Permissão de usuário atualizada com sucesso.",
        "user": _serialize_user_summary(user),
    })


@require_http_methods(["POST"])
@never_cache
@transaction.atomic
def admin_user_status(request, user_id):
    try:
        actor = _require_admin_user(request)
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

    if not isinstance(payload, dict) or "is_active" not in payload:
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
        user = (
            User.objects.select_for_update()
            .select_related("profile")
            .get(pk=user_id)
        )
    except User.DoesNotExist:
        return JsonResponse({"error": "Usuário não encontrado."}, status=404)

    target_role = _role_for_user(user)
    if (
        actor.profile.role == UserProfile.Role.STUDENT_ADMIN
        and target_role == UserProfile.Role.ADMIN
    ):
        return JsonResponse(
            {"error": "Estudantes-administradores não podem bloquear contas ADMIN."},
            status=403,
        )
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
        and current_role == UserProfile.Role.ADMIN
        and not payload["is_active"]
    ):
        remaining_admins = User.objects.filter(
            is_active=True,
            profile__role=UserProfile.Role.ADMIN,
        ).exclude(pk=user.pk).count()
        if remaining_admins == 0:
            return JsonResponse(
                {"error": "Não é possível bloquear o último ADMIN ativo."},
                status=400,
            )

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        return JsonResponse(
            {"error": "Informe uma justificativa obrigatória."},
            status=400,
        )
    if user.is_active != payload["is_active"]:
        user.is_active = payload["is_active"]
        user.save(update_fields=["is_active"])
        profile, _created = UserProfile.objects.get_or_create(
            user=user,
            defaults={"role": target_role},
        )
        profile.credential_revision += 1
        profile.save(update_fields=["credential_revision"])
        AccountAdminAudit.objects.create(
            actor=request.user,
            actor_identifier=request.user.pk,
            target=user,
            target_identifier=user.pk,
            action=(
                AccountAdminAudit.Action.ACCOUNT_REACTIVATED
                if user.is_active
                else AccountAdminAudit.Action.ACCOUNT_BLOCKED
            ),
            reason=reason.strip(),
        )
        revoke_user_sessions(user.pk)
    return JsonResponse({
        "message": "Status do usuário atualizado com sucesso.",
        "user": _serialize_user_summary(user),
    })


def _is_full_admin_user(user):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    try:
        return user.profile.role == UserProfile.Role.ADMIN
    except UserProfile.DoesNotExist:
        return False


@require_GET
@never_cache
def admin_user_history(request, user_id):
    if not _is_authorized_admin(request):
        return JsonResponse({"error": "Permissão administrativa necessária."}, status=403)
    user = User.objects.select_related("profile").filter(pk=user_id).first()
    if user is None:
        return JsonResponse({"error": "Usuário não encontrado."}, status=404)
    target_role = _role_for_user(user)
    if (
        request.user.profile.role == UserProfile.Role.STUDENT_ADMIN
        and target_role == UserProfile.Role.ADMIN
    ):
        return JsonResponse({"error": "Acesso negado."}, status=403)

    identities = list(
        ParticipationIdentity.objects.filter(
            original_user_id=user.pk,
        ).order_by("generation")
    )
    identity_ids = [identity.pk for identity in identities]
    return JsonResponse({
        "user_id": user.pk,
        "participations": [
            {
                "identity_id": identity.pk,
                "generation": identity.generation,
                "label": identity.display_label,
                "current": identity.is_current,
                "eligible": identity.is_eligible,
                "created_at": identity.created_at.isoformat(),
                "ended_at": (
                    identity.ended_at.isoformat()
                    if identity.ended_at
                    else None
                ),
                "responses": PreferenceResponse.objects.filter(
                    participation_identity=identity
                ).count(),
                "votes": Vote.objects.filter(
                    participation_identity=identity
                ).count(),
                "proposals": ShirtProposal.objects.filter(
                    author_identity=identity
                ).count(),
            }
            for identity in identities
        ],
        "participation_audit": [
            {
                "id": event.pk,
                "type": event.participation_type,
                "participation_id": event.participation_id,
                "action": event.action,
                "reason": event.reason,
                "actor_id": event.actor_identifier,
                "created_at": event.created_at.isoformat(),
            }
            for event in ParticipationAuditEvent.objects.filter(
                participation_identity_id__in=identity_ids
            ).order_by("-created_at")
        ],
        "account_audit": [
            {
                "id": event.pk,
                "action": event.action,
                "reason": event.reason,
                "actor_id": event.actor_identifier,
                "created_at": event.created_at.isoformat(),
            }
            for event in AccountAdminAudit.objects.filter(
                target_identifier=user.pk
            ).order_by("-created_at")
        ],
    })


def _participation_model(kind):
    return {
        "preference_response": (
            PreferenceResponse,
            ParticipationAuditEvent.ParticipationType.PREFERENCE_RESPONSE,
        ),
        "vote": (Vote, ParticipationAuditEvent.ParticipationType.VOTE),
        "proposal": (ShirtProposal, ParticipationAuditEvent.ParticipationType.PROPOSAL),
    }.get(kind)


def _participation_identity(record):
    if isinstance(record, PreferenceResponse):
        return record.participation_identity
    if isinstance(record, Vote):
        return record.participation_identity
    return record.author_identity


def _participation_is_invalidated(record):
    return record.is_invalidated


def _set_participation_invalidated(record, invalidated):
    record.is_invalidated = invalidated
    record.save(update_fields=["is_invalidated"])


def _participation_reason(request):
    try:
        payload = json.loads(request.body or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    if not isinstance(reason, str):
        return None
    reason = reason.strip()
    return reason if reason and len(reason) <= 1000 else None


def _record_participation_change(request, kind, record, action, reason):
    ParticipationAuditEvent.objects.create(
        participation_type=kind,
        participation_id=record.pk,
        participation_identity=_participation_identity(record),
        actor=request.user,
        actor_identifier=request.user.pk,
        action=action,
        reason=reason,
    )


@require_POST
@never_cache
@transaction.atomic
def invalidate_participation(request, kind, participation_id):
    if not _is_full_admin_user(request.user):
        return JsonResponse({"error": "A invalidação exige papel ADMIN."}, status=403)
    reason = _participation_reason(request)
    if reason is None:
        return JsonResponse({"error": "Informe uma justificativa obrigatória."}, status=400)
    mapping = _participation_model(kind)
    if mapping is None:
        return JsonResponse({"error": "Tipo de participação inválido."}, status=400)
    model, participation_type = mapping
    try:
        settings_row = (
            ElectionSettings.objects.select_for_update()
            .filter(pk=1)
            .first()
        )
        record = model.objects.select_for_update().get(pk=participation_id)
        if _participation_is_invalidated(record):
            return JsonResponse({"error": "A participação já está invalidada."}, status=409)
        identity = _participation_identity(record)
        if (
            settings_row
            and settings_row.results_release_authorized
            and not settings_row.voting_open
        ):
            create_result_snapshot(request.user, "participation_invalidation_before")
        _set_participation_invalidated(record, True)
        _record_participation_change(
            request,
            participation_type,
            record,
            ParticipationAuditEvent.Action.INVALIDATE,
            reason,
        )
        if (
            settings_row
            and settings_row.results_release_authorized
            and not settings_row.voting_open
        ):
            create_result_snapshot(request.user, "participation_invalidation_after")
    except model.DoesNotExist:
        return JsonResponse({"error": "Participação não encontrada."}, status=404)
    except ValidationError as error:
        return _validation_error_response(error)
    except DatabaseError:
        logger.exception("Falha ao invalidar participação eleitoral.")
        return JsonResponse({"error": "Não foi possível invalidar a participação."}, status=500)
    return JsonResponse({
        "message": "Participação invalidada sem excluir o registro.",
        "participation_id": record.pk,
        "identity_id": identity.pk if identity else None,
    })


@require_POST
@never_cache
@transaction.atomic
def restore_participation(request, kind, participation_id):
    if not _is_full_admin_user(request.user):
        return JsonResponse({"error": "A reversão exige papel ADMIN."}, status=403)
    reason = _participation_reason(request)
    if reason is None:
        return JsonResponse({"error": "Informe uma justificativa obrigatória."}, status=400)
    mapping = _participation_model(kind)
    if mapping is None:
        return JsonResponse({"error": "Tipo de participação inválido."}, status=400)
    model, participation_type = mapping
    try:
        settings_row = (
            ElectionSettings.objects.select_for_update()
            .filter(pk=1)
            .first()
        )
        record = model.objects.select_for_update().get(pk=participation_id)
        if not _participation_is_invalidated(record):
            return JsonResponse({"error": "A participação não está invalidada."}, status=409)
        identity = _participation_identity(record)
        if (
            settings_row
            and settings_row.results_release_authorized
            and not settings_row.voting_open
        ):
            create_result_snapshot(request.user, "participation_restoration_before")
        try:
            with transaction.atomic():
                _set_participation_invalidated(record, False)
        except IntegrityError:
            return JsonResponse(
                {"error": "A restauração criaria mais de um voto válido para a geração."},
                status=409,
            )
        _record_participation_change(
            request,
            participation_type,
            record,
            ParticipationAuditEvent.Action.RESTORE,
            reason,
        )
        if (
            settings_row
            and settings_row.results_release_authorized
            and not settings_row.voting_open
        ):
            create_result_snapshot(request.user, "participation_restoration_after")
    except model.DoesNotExist:
        return JsonResponse({"error": "Participação não encontrada."}, status=404)
    except ValidationError as error:
        return _validation_error_response(error)
    except DatabaseError:
        logger.exception("Falha ao restaurar participação eleitoral.")
        return JsonResponse({"error": "Não foi possível restaurar a participação."}, status=500)
    return JsonResponse({"message": "Restauração registrada em auditoria."})


@require_POST
@never_cache
@transaction.atomic
def authorize_replacement_vote(request, user_id):
    if not _is_full_admin_user(request.user):
        return JsonResponse({"error": "A autorização exige papel ADMIN."}, status=403)
    reason = _participation_reason(request)
    if reason is None:
        return JsonResponse({"error": "Informe uma justificativa obrigatória."}, status=400)
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if user is None:
        return JsonResponse({"error": "Usuário não encontrado."}, status=404)
    identity = ParticipationIdentity.objects.select_for_update().filter(
        user=user,
        is_current=True,
    ).first()
    if identity is None or not identity.is_eligible:
        return JsonResponse({"error": "A conta não possui uma participação elegível."}, status=409)
    if Vote.objects.filter(
        participation_identity=identity,
        is_invalidated=False,
    ).exists():
        return JsonResponse({"error": "A geração já possui um voto válido."}, status=409)
    invalid_vote = Vote.objects.filter(
        participation_identity=identity,
        is_invalidated=True,
    ).order_by("-created_at").first()
    if invalid_vote is None:
        return JsonResponse({"error": "Não há voto invalidado para substituir."}, status=409)
    authorization, created = VoteReplacementAuthorization.objects.get_or_create(
        participation_identity=identity,
        consumed_at__isnull=True,
        defaults={
            "actor": request.user,
            "actor_identifier": request.user.pk,
            "reason": reason,
        },
    )
    if not created:
        return JsonResponse({"error": "Já existe uma autorização pendente."}, status=409)
    ParticipationAuditEvent.objects.create(
        participation_type=ParticipationAuditEvent.ParticipationType.VOTE,
        participation_id=invalid_vote.pk,
        participation_identity=identity,
        actor=request.user,
        actor_identifier=request.user.pk,
        action=ParticipationAuditEvent.Action.AUTHORIZE_REPLACEMENT_VOTE,
        reason=reason,
    )
    return JsonResponse({
        "message": "A autorização foi registrada e será consumida no próximo voto.",
    })


@require_POST
@never_cache
def admin_maintenance_preview(request):
    if not _is_authorized_admin(request):
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
                {"error": "A reinicialização eleitoral não aceita um alvo."},
                status=400,
            )
    elif type(target_id) is not int or target_id < 1:
        return JsonResponse(
            {"error": "Selecione um registro válido."},
            status=400,
        )
    reason = payload.get("reason")
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or len(reason.strip()) > 1000
    ):
        return JsonResponse(
            {"error": "Informe uma justificativa obrigatória."},
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
                actor_identifier=request.user.pk,
                token_digest=token_digest,
                action=action,
                target_id=plan["target_id"],
                summary=plan["audit_counts"],
                snapshot_digest=plan["snapshot_digest"],
                expires_at=expires_at,
                reason=reason.strip(),
            )
    except (User.DoesNotExist, ShirtProposal.DoesNotExist):
        return JsonResponse(
            {"error": "O registro selecionado não foi encontrado."},
            status=404,
        )
    except ValidationError as exc:
        return _validation_error_response(exc)
    except PermissionDenied as exc:
        return JsonResponse({"error": str(exc)}, status=403)
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
    return JsonResponse(response)


@require_POST
@never_cache
def admin_maintenance_confirm(request):
    if not _is_authorized_admin(request):
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
    backup = None
    attempted_action = None
    attempted_target_id = None
    attempted_reason = ""

    try:
        preview_confirmation = MaintenanceConfirmation.objects.filter(
            token_digest=token_digest,
            actor=request.user,
        ).first()
        if preview_confirmation is None:
            return JsonResponse(
                {"error": "A confirmação não é válida, expirou ou já foi usada."},
                status=409,
            )
        if preview_confirmation.expires_at <= timezone.now():
            preview_confirmation.delete()
            return JsonResponse(
                {"error": "A confirmação expirou. Gere uma nova prévia."},
                status=410,
            )
        if (
            preview_confirmation.action
            == MaintenanceConfirmation.Action.RESET_ELECTION
            and payload.get("confirmation_phrase")
            != ELECTION_RESET_CONFIRMATION
        ):
            return JsonResponse(
                {"error": "Digite exatamente REINICIAR ELEIÇÃO para confirmar."},
                status=400,
            )
        try:
            preview_plan = _build_maintenance_plan(
                preview_confirmation.action,
                preview_confirmation.target_id,
                request.user,
            )
        except (User.DoesNotExist, ShirtProposal.DoesNotExist):
            preview_confirmation.delete()
            return JsonResponse(
                {"error": "A prévia ficou desatualizada. Gere uma nova."},
                status=409,
            )
        except ValidationError:
            preview_confirmation.delete()
            return JsonResponse(
                {"error": "A operação não é mais permitida. Gere uma nova prévia."},
                status=409,
            )
        except PermissionDenied as exc:
            preview_confirmation.delete()
            return JsonResponse({"error": str(exc)}, status=403)
        if (
            preview_plan["snapshot_digest"]
            != preview_confirmation.snapshot_digest
            or preview_plan["audit_counts"] != preview_confirmation.summary
        ):
            preview_confirmation.delete()
            return JsonResponse(
                {"error": "Os dados mudaram desde a prévia. Gere uma nova."},
                status=409,
            )
        attempted_action = preview_confirmation.action
        attempted_target_id = preview_plan.get("audit_target_identifier")
        attempted_reason = preview_confirmation.reason
        backup = _create_verified_database_backup()

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
            except PermissionDenied as exc:
                confirmation.delete()
                return JsonResponse({"error": str(exc)}, status=403)

            if (
                plan["snapshot_digest"] != confirmation.snapshot_digest
                or plan["audit_counts"] != confirmation.summary
            ):
                confirmation.delete()
                return JsonResponse(
                    {"error": "Os dados mudaram desde a prévia. Gere uma nova."},
                    status=409,
                )

            reason = confirmation.reason
            attempted_action = confirmation.action
            attempted_target_id = plan.get("audit_target_identifier")
            attempted_reason = reason
            _apply_maintenance_action(
                confirmation.action,
                confirmation.target_id,
                plan,
                request.user,
                reason,
            )
            completed_action = confirmation.action
            affected_counts = plan["audit_counts"]
            warning = plan["warning"]
            MaintenanceAudit.objects.create(
                actor=request.user,
                actor_identifier=request.user.pk,
                target_identifier=attempted_target_id,
                action=completed_action,
                affected_counts=affected_counts,
                reason=reason,
                result="completed",
                details={"backup": backup},
            )
            MaintenanceConfirmation.objects.exclude(
                pk=confirmation.pk
            ).delete()
            confirmation.delete()
    except MaintenanceBackupError:
        if attempted_action is not None:
            _record_failed_maintenance(
                request.user,
                attempted_action,
                attempted_target_id,
                attempted_reason,
                "backup_failed",
            )
        return JsonResponse(
            {
                "error": (
                    "A operação foi bloqueada porque não foi possível "
                    "criar e verificar o backup."
                )
            },
            status=503,
        )
    except ValidationError as exc:
        return _validation_error_response(exc)
    except DatabaseError:
        logger.exception("Falha ao executar uma operação de manutenção.")
        if attempted_action is not None:
            _record_failed_maintenance(
                request.user,
                attempted_action,
                attempted_target_id,
                attempted_reason,
                "failed",
            )
        return JsonResponse(
            {"error": "Não foi possível concluir a operação de manutenção."},
            status=500,
        )

    cleanup_failures = _process_maintenance_file_cleanup()
    message = {
        MaintenanceConfirmation.Action.RESET_USER_BASIC:
            "Conta reinicializada; o histórico eleitoral foi preservado.",
        MaintenanceConfirmation.Action.RESET_USER_FULL:
            "Conta reinicializada; participações da conta removidas quando permitido e histórico de terceiros preservado.",
        MaintenanceConfirmation.Action.DELETE_USER:
            "Conta excluída; identidades e registros de auditoria foram preservados.",
        MaintenanceConfirmation.Action.DELETE_PROPOSAL:
            "Proposta e votos associados excluídos; auditoria e snapshots preservados.",
        MaintenanceConfirmation.Action.RESET_ELECTION:
            "Dados operacionais da eleição reiniciados; identidades, auditorias e snapshots preservados.",
    }[completed_action]
    return JsonResponse({
        "message": message,
        "affected": affected_counts,
        "warning": warning,
        "file_cleanup_pending": cleanup_failures > 0,
        "backup": backup,
    })


def _record_failed_maintenance(actor, action, target_id, reason, result):
    try:
        MaintenanceAudit.objects.create(
            actor=actor,
            actor_identifier=actor.pk,
            target_identifier=target_id,
            action=action,
            affected_counts={},
            reason=reason,
            result=result,
            details={"backup": "unavailable"}
            if result == "backup_failed"
            else {},
        )
    except DatabaseError:
        logger.exception("Falha ao registrar resultado de manutenção.")


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
    active_students = ParticipationIdentity.objects.filter(
        is_eligible=True
    ).count()
    preferences_count = PreferenceResponse.objects.filter(
        is_invalidated=False,
        participation_identity__is_eligible=True,
    ).count()
    proposal_counts = {
        status: ShirtProposal.objects.filter(status=status).count()
        for status in (
            ShirtProposal.Status.PENDING,
            ShirtProposal.Status.APPROVED,
            ShirtProposal.Status.REJECTED,
        )
    }
    vote_tally = get_vote_tally()
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
        "total_votes": vote_tally["total_votes"],
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
        identity = (
            ParticipationIdentity.objects.filter(
                user=request.user,
                is_current=True,
            ).first()
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
                Vote.objects.filter(
                    participation_identity=identity,
                    is_invalidated=False,
                ).exists()
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
                is_invalidated=False,
            ).order_by("-created_at", "-pk")
            if (
                _is_active_admin_user(request.user)
                and not _is_authorized_student(request.user)
            )
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
                "author_id": (
                    proposal.author_identity.user_id
                    if proposal.author_identity_id
                    and proposal.author_identity.is_current
                    else -1
                ),
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

        identity = ParticipationIdentity.objects.get(
            user=request.user,
            is_current=True,
        )
        proposals = ShirtProposal.objects.filter(
            author_identity=identity,
        ).values(
            "id",
            "title",
            "description",
            "status",
            "created_at",
            "is_invalidated",
        )
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
        snapshot = ElectionResultSnapshot.objects.order_by(
            "-sequence"
        ).first()
        if snapshot is None:
            snapshot = create_result_snapshot(
                request.user,
                "initial_release",
            )
    except PermissionDenied:
        return JsonResponse(
            {"error": "Acesso permitido apenas a estudantes ativos."},
            status=403,
        )
    except ValidationError as error:
        return _validation_error_response(error)
    except DatabaseError:
        logger.exception("Falha ao apurar resultados para estudantes.")
        return JsonResponse(
            {"error": "Não foi possível consultar os resultados."},
            status=500,
        )
    except OSError:
        logger.exception("Falha ao arquivar imagens dos resultados.")
        return JsonResponse(
            {"error": "Não foi possível preservar as imagens dos resultados."},
            status=500,
        )

    return JsonResponse(snapshot.payload)


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
@never_cache
def proposal_image(request, proposal_id):
    proposal = get_object_or_404(ShirtProposal, pk=proposal_id)

    if proposal.is_invalidated and not _is_authorized_admin(request):
        return HttpResponseForbidden()
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


@require_GET
@never_cache
def result_snapshot_image(request, sequence, proposal_id):
    if not _is_authorized_admin(request) and not (
        _is_authorized_student(request.user)
        and is_results_release_authorized()
        and not ElectionSettings.objects.filter(
            pk=1,
            voting_open=True,
        ).exists()
    ):
        return HttpResponseForbidden()

    snapshot = get_result_snapshot(sequence)
    if snapshot is None:
        raise Http404
    asset = snapshot.image_manifest.get(str(proposal_id))
    if not isinstance(asset, dict):
        raise Http404
    name = asset.get("name")
    expected_digest = asset.get("sha256")
    content_type = asset.get("content_type")
    if (
        not isinstance(name, str)
        or not isinstance(expected_digest, str)
        or content_type not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }
    ):
        raise Http404

    storage = PrivateProposalStorage(
        prefix="election_result_snapshots",
    )
    try:
        image_file = storage.open(name, "rb")
        image_bytes = image_file.read()
        image_file.close()
    except FileNotFoundError as exc:
        raise Http404 from exc
    if not hmac.compare_digest(
        hashlib.sha256(image_bytes).hexdigest(),
        expected_digest,
    ):
        logger.error(
            "Snapshot image integrity check failed for sequence %s.",
            sequence,
        )
        return JsonResponse(
            {"error": "A imagem arquivada não passou pela verificação de integridade."},
            status=500,
        )
    response = FileResponse(
        io.BytesIO(image_bytes),
        as_attachment=False,
        filename=f"result-{sequence}-proposal-{proposal_id}",
        content_type=content_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
