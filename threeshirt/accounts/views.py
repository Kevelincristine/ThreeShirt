
import hashlib
import hmac
import ipaddress
import logging
import secrets
import sqlite3
import time
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout as auth_logout
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.mail import send_mail
from django.db import OperationalError, connection, transaction
from django.db.models import F, Q
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import EmailLoginCode, LoginRequestRateBucket, UserProfile


LOGIN_CODE_MAX_ATTEMPTS = 5
LOGIN_REQUEST_WINDOW = timedelta(minutes=1)
LOGIN_REQUEST_ACCOUNT_LIMIT = 3
LOGIN_REQUEST_SOURCE_LIMIT = 10
SQLITE_RATE_LIMIT_RETRIES = 20

logger = logging.getLogger(__name__)


@require_GET
@ensure_csrf_cookie
def initialize_csrf(request):
    """Provides a CSRF token to the frontend without changing authentication."""
    return JsonResponse({"csrfToken": get_token(request)})


@require_GET
def session_status(request):
    """Returns the authenticated user's public identity, if available."""
    user = request.user

    if not user.is_authenticated:
        return JsonResponse({"authenticated": False})

    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        return JsonResponse(
            {"error": "Perfil de usuário não encontrado."},
            status=500,
        )

    role = {
        UserProfile.Role.STUDENT: "student",
        UserProfile.Role.STUDENT_ADMIN: "student_admin",
        UserProfile.Role.ADMIN: "admin",
    }.get(profile.role)

    if role is None:
        return JsonResponse(
            {"error": "Perfil de usuário inválido."},
            status=500,
        )

    return JsonResponse({
        "authenticated": True,
        "user": {
            "id": user.pk,
            "email": user.email,
            "role": role,
        },
    })


@require_POST
def logout_user(request):
    """Ends the current Django session."""
    auth_logout(request)

    return JsonResponse({
        "authenticated": False,
        "message": "Sessão encerrada com sucesso.",
    })


def _run_with_sqlite_lock_retry(operation):
    """Retry transient SQLite locks and propagate other or exhausted errors."""
    for attempt in range(SQLITE_RATE_LIMIT_RETRIES):
        try:
            return operation()

        except OperationalError as exc:
            error_code = getattr(exc.__cause__, "sqlite_errorcode", None)

            primary_code = (
                error_code & 0xFF
                if error_code is not None
                else None
            )

            is_sqlite_lock = (
                connection.vendor == "sqlite"
                and primary_code in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
            )

            if (
                not is_sqlite_lock
                or attempt == SQLITE_RATE_LIMIT_RETRIES - 1
            ):
                raise

            time.sleep(
                min(0.005 * (2 ** attempt), 0.1)
                + secrets.randbelow(10) / 1000
            )


def _reserve_login_attempt(login_code, now):
    """Atomically reserve one of the code's limited verification attempts."""
    return _run_with_sqlite_lock_retry(
        lambda: (
            EmailLoginCode.objects
            .filter(
                pk=login_code.pk,
                used_at__isnull=True,
                expires_at__gt=now,
                attempts__lt=LOGIN_CODE_MAX_ATTEMPTS,
            )
            .update(attempts=F("attempts") + 1)
            == 1
        )
    )


def _mark_login_code_used(login_code, now):
    """Consume a code only if no other request has consumed it already."""
    return _run_with_sqlite_lock_retry(
        lambda: EmailLoginCode.objects.filter(
            pk=login_code.pk,
            used_at__isnull=True,
        ).update(used_at=now) == 1
    )


def _consume_login_code(login_code, now):
    """Atomically consume a code that is still valid and unused."""
    return _run_with_sqlite_lock_retry(
        lambda: EmailLoginCode.objects.filter(
            pk=login_code.pk,
            used_at__isnull=True,
            expires_at__gt=now,
        ).update(used_at=now) == 1
    )


def _rate_limit_digest(kind, identifier):
    message = f"threeshirt:login-request:{kind}:{identifier}".encode()

    return hmac.new(
        settings.SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()


def _request_source(request):
    """Use the address supplied by the WSGI server, never forwarded headers."""
    remote_addr = request.META.get("REMOTE_ADDR", "")

    try:
        address = ipaddress.ip_address(remote_addr)
    except (TypeError, ValueError):
        return "unknown"

    if isinstance(address, ipaddress.IPv6Address):
        return str(address.ipv4_mapped or address)

    return str(address)


def _record_login_request(email, source, now):
    """Atomically check and record account and source sliding-window limits."""
    return _run_with_sqlite_lock_retry(
        lambda: _record_login_request_once(email, source, now)
    )


def _record_login_request_once(email, source, now):
    """Apply one rate-limit update inside a database transaction."""
    source_digest = _rate_limit_digest("source", source)

    account_digest = (
        _rate_limit_digest("account", email)
        if email is not None
        else None
    )

    digests = sorted(
        digest
        for digest in (account_digest, source_digest)
        if digest is not None
    )

    cutoff = now - LOGIN_REQUEST_WINDOW
    cutoff_timestamp = int(cutoff.timestamp() * 1_000_000)

    with transaction.atomic():
        LoginRequestRateBucket.objects.filter(
            updated_at__lt=cutoff,
        ).delete()

        for digest in digests:
            LoginRequestRateBucket.objects.get_or_create(
                key_digest=digest,
            )

        buckets = {
            bucket.key_digest: bucket
            for bucket in (
                LoginRequestRateBucket.objects
                .select_for_update()
                .filter(key_digest__in=digests)
                .order_by("key_digest")
            )
        }

        recent = {
            digest: [
                timestamp
                for timestamp in bucket.request_timestamps
                if timestamp >= cutoff_timestamp
            ]
            for digest, bucket in buckets.items()
        }

        source_requests = recent[source_digest]

        if len(source_requests) >= LOGIN_REQUEST_SOURCE_LIMIT:
            return False

        account_limited = (
            account_digest is not None
            and len(recent[account_digest]) >= LOGIN_REQUEST_ACCOUNT_LIMIT
        )

        timestamp = int(now.timestamp() * 1_000_000)

        source_requests.append(timestamp)

        LoginRequestRateBucket.objects.filter(
            key_digest=source_digest,
        ).update(
            request_timestamps=source_requests,
            updated_at=now,
        )

        if account_digest is not None and not account_limited:
            recent[account_digest].append(timestamp)

            LoginRequestRateBucket.objects.filter(
                key_digest=account_digest,
            ).update(
                request_timestamps=recent[account_digest],
                updated_at=now,
            )

        return not account_limited


def _activate_login_code(email, pending_code):
    """Atomically replace active codes after delivery is confirmed."""
    now = timezone.now()
    account_digest = _rate_limit_digest("account", email)

    with transaction.atomic():
        LoginRequestRateBucket.objects.select_for_update().get(
            key_digest=account_digest,
        )

        EmailLoginCode.objects.filter(
            email=email,
            used_at__isnull=True,
        ).exclude(
            pk=pending_code.pk,
        ).update(
            used_at=now,
        )

        activated = EmailLoginCode.objects.filter(
            pk=pending_code.pk,
            used_at__isnull=False,
        ).update(
            used_at=None,
            expires_at=now + timedelta(minutes=10),
        )

        if activated != 1:
            raise RuntimeError(
                "O código de acesso pendente não foi ativado."
            )


@require_POST
def request_login_code(request):
    """Solicita um código temporário de acesso."""

    email = request.POST.get("email", "").strip().lower()

    def response(state, message):
        return JsonResponse({
            "state": state,
            "message": message,
        })

    valid_email = True

    try:
        validate_email(email)
    except ValidationError:
        valid_email = False

    allowed_emails = {
        address.strip().lower()
        for address in settings.AUTHORIZED_LOGIN_EMAILS
    }

    is_authorized = valid_email and email in allowed_emails
    rate_limit_email = email if is_authorized else None

    within_rate_limit = _record_login_request(
        rate_limit_email,
        _request_source(request),
        timezone.now(),
    )

    if not valid_email:
        return response(
            "invalid_email",
            "Digite um endereço de e-mail válido.",
        )

    if not is_authorized:
        return response(
            "unauthorized",
            "Este e-mail não está autorizado a acessar o ThreeShirt. "
            "Utilize seu e-mail escolar autorizado.",
        )

    if not within_rate_limit:
        return response(
            "rate_limited",
            "Limite de solicitações atingido. Aguarde um momento e tente "
            "novamente.",
        )

    User = get_user_model()

    try:
        has_active_account = _run_with_sqlite_lock_retry(
            lambda: User.objects.filter(
                email__iexact=email,
                is_active=True,
            ).exists()
        )

    except Exception as exc:
        logger.error(
            "Falha ao verificar elegibilidade para código de acesso (%s).",
            type(exc).__name__,
        )

        return response(
            "send_failed",
            "Não foi possível processar a solicitação. Tente novamente.",
        )

    if not has_active_account:
        return response(
            "account_unavailable",
            "Não há uma conta ativa disponível para este e-mail.",
        )

    try:
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = timezone.now()

        pending_code = _run_with_sqlite_lock_retry(
            lambda: EmailLoginCode.objects.create(
                email=email,
                code_hash=make_password(code),
                expires_at=now + timedelta(minutes=10),
                used_at=now,
            )
        )

    except Exception as exc:
        logger.error(
            "Falha ao preparar código de acesso (%s).",
            type(exc).__name__,
        )

        return response(
            "send_failed",
            "Não foi possível enviar o código. Tente novamente.",
        )

    try:
        sent_count = send_mail(
            subject="Seu código de acesso ao ThreeShirt",
            message=(
                f"Seu código de acesso é: {code}\n\n"
                "Ele expira em 10 minutos. "
                "Se você não solicitou este código, ignore esta mensagem."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )

    except Exception as exc:
        logger.error(
            "Resultado do envio do código de acesso não confirmado (%s).",
            type(exc).__name__,
        )

        return response(
            "send_failed",
            "Não foi possível enviar o código. Tente novamente.",
        )

    if sent_count != 1:
        logger.error("O envio do código de acesso não foi confirmado.")

        return response(
            "send_failed",
            "Não foi possível enviar o código. Tente novamente.",
        )

    try:
        _run_with_sqlite_lock_retry(
            lambda: _activate_login_code(email, pending_code)
        )

    except Exception as exc:
        logger.error(
            "E-mail do código enviado, mas ativação no banco não confirmada "
            "(%s).",
            type(exc).__name__,
        )

        return response(
            "send_failed",
            "Não foi possível concluir o envio. Tente novamente.",
        )

    return response(
        "enter_code",
        "Solicitação aceita pelo serviço de e-mail. Confira sua caixa de "
        "entrada e a pasta de spam.",
    )


@require_POST
def verify_login_code(request):
    """Valida o código temporário e inicia a sessão do usuário."""

    email = request.POST.get("email", "").strip().lower()
    code = request.POST.get("code", "").strip()

    invalid_response = {
        "error": "Código inválido ou expirado."
    }

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse(invalid_response, status=400)

    # Exige exatamente seis dígitos ASCII.
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return JsonResponse(invalid_response, status=400)

    login_code = (
        EmailLoginCode.objects
        .filter(
            email=email,
            used_at__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )

    if login_code is None:
        return JsonResponse(invalid_response, status=400)

    now = timezone.now()

    # Rejeita códigos expirados.
    if login_code.expires_at <= now:
        _mark_login_code_used(login_code, now)

        return JsonResponse(invalid_response, status=400)

    # Reserva uma tentativa de validação de forma atômica.
    if not _reserve_login_attempt(login_code, now):
        EmailLoginCode.objects.filter(
            pk=login_code.pk,
            used_at__isnull=True,
        ).filter(
            Q(expires_at__lte=now)
            | Q(attempts__gte=LOGIN_CODE_MAX_ATTEMPTS)
        ).update(
            used_at=now,
        )

        return JsonResponse(invalid_response, status=400)

    if not check_password(code, login_code.code_hash):
        return JsonResponse(invalid_response, status=400)

    # Confirma que o endereço continua autorizado.
    allowed_emails = {
        address.strip().lower()
        for address in settings.AUTHORIZED_LOGIN_EMAILS
    }

    if email not in allowed_emails:
        _mark_login_code_used(login_code, now)

        return JsonResponse(invalid_response, status=400)

    # Localiza a conta existente no Django.
    User = get_user_model()

    user = _run_with_sqlite_lock_retry(
        lambda: User.objects.filter(
            email__iexact=email,
        ).first()
    )

    if user is None or not user.is_active:
        _mark_login_code_used(login_code, now)

        return JsonResponse(invalid_response, status=400)

    # Apenas uma solicitação concorrente pode consumir o código.
    if not _consume_login_code(login_code, timezone.now()):
        return JsonResponse(invalid_response, status=400)

    # Lista usada somente para configurar o primeiro administrador
    # quando a conta ainda não possui um perfil.
    admin_emails = {
        address.strip().lower()
        for address in settings.ADMIN_EMAILS
    }

    initial_role = (
        UserProfile.Role.ADMIN
        if email in admin_emails
        else UserProfile.Role.STUDENT
    )

    # Perfis existentes preservam o papel já salvo no banco.
    profile, _ = _run_with_sqlite_lock_retry(
        lambda: UserProfile.objects.get_or_create(
            user=user,
            defaults={"role": initial_role},
        )
    )

    # Inicia a sessão autenticada do Django.
    _run_with_sqlite_lock_retry(
        lambda: login(request, user)
    )

    return JsonResponse({
        "message": "Login realizado com sucesso."
    })
