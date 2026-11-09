import hashlib
import hmac
import ipaddress
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout as auth_logout,
    update_session_auth_hash,
)
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import (
    AccountAdminAudit,
    AuthRateLimitBucket,
    UserProfile,
)
from .access import (
    is_authorized_admin_email,
    is_authorized_login_email,
    is_authorized_session_user,
    normalize_authorized_email,
)
from .account_sessions import revoke_user_sessions

logger = logging.getLogger(__name__)
User = get_user_model()

AUTH_WINDOW = timedelta(minutes=5)
AUTH_EMAIL_LIMIT = 8
AUTH_SOURCE_LIMIT = 24


def _request_data(request):
    if request.content_type and "application/json" in request.content_type:
        try:
            data = json.loads(request.body or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None
    return request.POST


def _normalize_email(value):
    return normalize_authorized_email(value)


def _authorized_email(email):
    return is_authorized_login_email(email)


def _find_unique_account(email):
    matches = list(
        User.objects.filter(email__iexact=email).order_by("pk")[:2]
    )
    return matches[0] if len(matches) == 1 else None


def _bucket_digest(kind, value):
    message = f"threeshirt:auth:{kind}:{value}".encode("utf-8")
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def _request_source(request):
    remote_addr = request.META.get("REMOTE_ADDR", "")
    try:
        address = ipaddress.ip_address(remote_addr)
    except (TypeError, ValueError):
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        return str(address.ipv4_mapped or address)
    return str(address)


def _record_auth_attempt(request, email, purpose):
    now = timezone.now()
    cutoff = now - AUTH_WINDOW
    cutoff_timestamp = int(cutoff.timestamp() * 1_000_000)
    source_digest = _bucket_digest(
        f"{purpose}:source",
        _request_source(request),
    )
    email_digest = (
        _bucket_digest(f"{purpose}:email", email)
        if email
        else None
    )
    digests = sorted(
        digest for digest in (source_digest, email_digest) if digest
    )

    with transaction.atomic():
        AuthRateLimitBucket.objects.filter(updated_at__lt=now - timedelta(days=1)).delete()
        for digest in digests:
            AuthRateLimitBucket.objects.get_or_create(key_digest=digest)

        buckets = {
            bucket.key_digest: bucket
            for bucket in (
                AuthRateLimitBucket.objects.select_for_update()
                .filter(key_digest__in=digests)
                .order_by("key_digest")
            )
        }
        recent = {
            digest: [
                timestamp
                for timestamp in buckets[digest].request_timestamps
                if timestamp >= cutoff_timestamp
            ]
            for digest in digests
        }
        allowed = (
            len(recent[source_digest]) < AUTH_SOURCE_LIMIT
            and (
                email_digest is None
                or len(recent[email_digest]) < AUTH_EMAIL_LIMIT
            )
        )
        timestamp = int(now.timestamp() * 1_000_000)
        for digest in digests:
            values = recent[digest]
            values.append(timestamp)
            AuthRateLimitBucket.objects.filter(key_digest=digest).update(
                request_timestamps=values,
                updated_at=now,
            )
        return allowed


def _ensure_profile(user, email):
    profile, _created = UserProfile.objects.get_or_create(
        user=user,
        defaults={
            "role": (
                UserProfile.Role.ADMIN
                if is_authorized_admin_email(email)
                else UserProfile.Role.STUDENT
            )
        },
    )
    return profile


def _session_user_payload(user):
    profile = getattr(user, "profile", None)
    if profile is None:
        return None
    role = {
        UserProfile.Role.STUDENT: "student",
        UserProfile.Role.STUDENT_ADMIN: "student_admin",
        UserProfile.Role.ADMIN: "admin",
    }.get(profile.role)
    if role is None:
        return None
    return {"id": user.pk, "email": user.email, "role": role}


@require_GET
@ensure_csrf_cookie
def initialize_csrf(request):
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
@never_cache
def session_status(request):
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return JsonResponse({"authenticated": False})
    email = _normalize_email(user.email)
    if not email or not is_authorized_session_user(user):
        auth_logout(request)
        return JsonResponse({"authenticated": False})

    try:
        payload = _session_user_payload(user)
    except UserProfile.DoesNotExist:
        payload = None
    if payload is None:
        auth_logout(request)
        return JsonResponse(
            {"error": "Perfil de usuário inválido."},
            status=500,
        )
    return JsonResponse({"authenticated": True, "user": payload})


@require_POST
@never_cache
def login_with_password(request):
    data = _request_data(request)
    email = _normalize_email(data.get("email")) if data else None
    password = data.get("password") if data else None
    if not isinstance(password, str) or not password or len(password) > 1024:
        password = ""

    try:
        within_limit = _record_auth_attempt(request, email, "login")
        if not within_limit:
            return JsonResponse(
                {"error": "Muitas tentativas. Aguarde e tente novamente."},
                status=429,
            )
        user = (
            _find_unique_account(email)
            if email and _authorized_email(email)
            else None
        )
        candidate = authenticate(
            request,
            username=user.get_username() if user else (email or ""),
            password=password,
        )
        if (
            user is None
            or candidate is None
            or candidate.pk != user.pk
        ):
            return JsonResponse(
                {"error": "E-mail ou senha inválidos."},
                status=400,
            )
        with transaction.atomic():
            locked_user = User.objects.select_for_update().get(pk=candidate.pk)
            if (
                not locked_user.is_active
                or not _authorized_email(email)
                or _normalize_email(locked_user.email) != email
                or not locked_user.check_password(password)
            ):
                return JsonResponse(
                    {"error": "E-mail ou senha inválidos."},
                    status=400,
                )
            _ensure_profile(locked_user, email)
            login(request, locked_user)
    except DatabaseError:
        logger.exception("Falha ao autenticar conta por senha.")
        return JsonResponse(
            {"error": "Não foi possível concluir o acesso."},
            status=503,
        )
    return JsonResponse({"message": "Login realizado com sucesso."})


def _record_first_access_audit(user, action, reason, target_digest=None):
    details = {
        "method": "self_service_first_access",
        "result": (
            "completed"
            if action == AccountAdminAudit.Action.FIRST_ACCESS_COMPLETED
            else "rejected"
        ),
    }
    if target_digest:
        details["target_digest"] = target_digest
    AccountAdminAudit.objects.create(
        actor=None,
        actor_identifier=0,
        target=user,
        target_identifier=user.pk if user else 0,
        action=action,
        reason=reason,
        details=details,
    )


@require_POST
@never_cache
def first_access(request):
    data = _request_data(request)
    email = _normalize_email(data.get("email")) if data else None
    password = data.get("password") if data else None
    password_confirmation = data.get("password_confirmation") if data else None
    if (
        not isinstance(password, str)
        or not password
        or len(password) > 1024
        or password != password_confirmation
    ):
        return JsonResponse(
            {"error": "Informe e confirme a nova senha."},
            status=400,
        )

    try:
        if not _record_auth_attempt(request, email, "first-access"):
            return JsonResponse(
                {"error": "Muitas tentativas. Aguarde e tente novamente."},
                status=429,
            )
        user = (
            _find_unique_account(email)
            if email and _authorized_email(email)
            else None
        )
        with transaction.atomic():
            if user is None:
                _record_first_access_audit(
                    None,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "Conta inexistente, não autorizada ou ambígua.",
                    _bucket_digest("first-access-audit", email) if email else None,
                )
                return JsonResponse(
                    {"error": "Conta indisponível para primeiro acesso."},
                    status=400,
                )

            user = User.objects.select_for_update().get(pk=user.pk)
            if not user.is_active:
                _record_first_access_audit(
                    user,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "Conta inativa.",
                )
                return JsonResponse(
                    {"error": "Conta indisponível para primeiro acesso."},
                    status=400,
                )
            try:
                profile = UserProfile.objects.select_for_update().get(user=user)
            except UserProfile.DoesNotExist:
                _record_first_access_audit(
                    user,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "Conta sem papel de usuário válido.",
                )
                return JsonResponse(
                    {"error": "Conta indisponível para primeiro acesso."},
                    status=400,
                )
            if profile.role not in UserProfile.Role.values:
                _record_first_access_audit(
                    user,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "Papel da conta inválido.",
                )
                return JsonResponse(
                    {"error": "Conta indisponível para primeiro acesso."},
                    status=400,
                )
            if (
                profile.role == UserProfile.Role.ADMIN
                and not is_authorized_admin_email(email)
            ):
                _record_first_access_audit(
                    user,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "E-mail não autorizado para papel ADMIN.",
                )
                return JsonResponse(
                    {"error": "Conta indisponível para primeiro acesso."},
                    status=400,
                )
            if user.has_usable_password():
                _record_first_access_audit(
                    user,
                    AccountAdminAudit.Action.FIRST_ACCESS_REJECTED,
                    "Conta já possui senha.",
                )
                return JsonResponse(
                    {
                        "error": (
                            "Esta conta já possui senha. Entre com ela pelo login normal."
                        )
                    },
                    status=409,
                )
            try:
                validate_password(password, user)
            except ValidationError as exc:
                return JsonResponse(
                    {
                        "error": "A nova senha não atende aos requisitos.",
                        "fields": {"password": exc.messages},
                    },
                    status=400,
                )

            user.set_password(password)
            user.save(update_fields=["password"])
            profile.credential_revision += 1
            profile.save(update_fields=["credential_revision"])
            _record_first_access_audit(
                user,
                AccountAdminAudit.Action.FIRST_ACCESS_COMPLETED,
                "Primeiro acesso concluído pelo titular da conta.",
            )
    except DatabaseError:
        logger.exception("Falha ao concluir o primeiro acesso.")
        return JsonResponse(
            {"error": "Não foi possível concluir o primeiro acesso."},
            status=503,
        )
    return JsonResponse(
        {"message": "Senha definida. Entre com seu e-mail e a nova senha."}
    )


@require_POST
@never_cache
def change_password(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Autenticação necessária."}, status=401)
    if not is_authorized_session_user(request.user):
        auth_logout(request)
        return JsonResponse({"error": "Autenticação necessária."}, status=401)
    data = _request_data(request)
    current_password = data.get("current_password") if data else None
    new_password = data.get("new_password") if data else None
    password_confirmation = data.get("password_confirmation") if data else None
    if (
        not isinstance(current_password, str)
        or not current_password
        or not isinstance(new_password, str)
        or not new_password
        or len(new_password) > 1024
        or new_password != password_confirmation
    ):
        return JsonResponse(
            {"error": "Informe e confirme as senhas solicitadas."},
            status=400,
        )

    try:
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            if not user.is_active or not user.check_password(current_password):
                return JsonResponse({"error": "Senha atual inválida."}, status=400)
            try:
                validate_password(new_password, user)
            except ValidationError as exc:
                return JsonResponse(
                    {"error": "A nova senha não atende aos requisitos.", "fields": {"new_password": exc.messages}},
                    status=400,
                )
            try:
                profile = UserProfile.objects.select_for_update().get(user=user)
            except UserProfile.DoesNotExist:
                return JsonResponse(
                    {"error": "A conta não possui um perfil válido."},
                    status=403,
                )
            if profile.role not in UserProfile.Role.values:
                return JsonResponse(
                    {"error": "A conta não possui um perfil válido."},
                    status=403,
                )
            user.set_password(new_password)
            user.save(update_fields=["password"])
            profile.credential_revision += 1
            profile.save(update_fields=["credential_revision"])
            from .signals import SESSION_CREDENTIAL_REVISION_KEY

            request.session[SESSION_CREDENTIAL_REVISION_KEY] = (
                profile.credential_revision
            )
            AccountAdminAudit.objects.create(
                actor=user,
                actor_identifier=user.pk,
                target=user,
                target_identifier=user.pk,
                action=AccountAdminAudit.Action.PASSWORD_CHANGED,
                reason="Troca de senha pelo titular.",
            )
            update_session_auth_hash(request, user)
            revoke_user_sessions(user.pk, request.session.session_key)
    except DatabaseError:
        logger.exception("Falha ao alterar senha da conta.")
        return JsonResponse({"error": "Não foi possível alterar a senha."}, status=503)
    return JsonResponse({"message": "Senha alterada com sucesso."})


@require_POST
def logout_user(request):
    auth_logout(request)
    return JsonResponse({
        "authenticated": False,
        "message": "Sessão encerrada com sucesso.",
    })


def _permission_denied_response(error):
    return JsonResponse({"error": str(error)}, status=403)
