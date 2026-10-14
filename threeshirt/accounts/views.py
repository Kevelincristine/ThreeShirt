
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.mail import send_mail
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import EmailLoginCode, UserProfile


@require_POST
def request_login_code(request):
    """Solicita um código temporário de acesso."""

    email = request.POST.get("email", "").strip().lower()

    response = {
        "message": (
            "Se o endereço estiver autorizado, "
            "você receberá instruções para continuar."
        )
    }

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse(response, status=200)

    allowed_emails = {
        address.strip().lower()
        for address in settings.AUTHORIZED_LOGIN_EMAILS
    }

    if email not in allowed_emails:
        return JsonResponse(response, status=200)

    # Limita solicitações repetidas para o mesmo endereço.
    one_minute_ago = timezone.now() - timedelta(minutes=1)

    recent_requests = EmailLoginCode.objects.filter(
        email=email,
        created_at__gte=one_minute_ago,
    ).count()

    if recent_requests >= 3:
        return JsonResponse(response, status=200)

    # Gera um código numérico de seis dígitos.
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = make_password(code)
    now = timezone.now()

    # Invalida códigos anteriores ainda não utilizados.
    EmailLoginCode.objects.filter(
        email=email,
        used_at__isnull=True,
    ).update(used_at=now)

    # Cria um código com validade de dez minutos.
    EmailLoginCode.objects.create(
        email=email,
        code_hash=code_hash,
        expires_at=now + timedelta(minutes=10),
    )

    # Envia o código usando a configuração de e-mail do Django.
    send_mail(
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

    return JsonResponse(response, status=200)


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
        .filter(email=email, used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )

    if login_code is None:
        return JsonResponse(invalid_response, status=400)

    now = timezone.now()

    # Rejeita códigos expirados.
    if login_code.expires_at <= now:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Limita a cinco tentativas por código.
    if login_code.attempts >= 5:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    login_code.attempts += 1
    login_code.save(update_fields=["attempts"])

    if not check_password(code, login_code.code_hash):
        return JsonResponse(invalid_response, status=400)

    # Confirma que o endereço continua autorizado.
    allowed_emails = {
        address.strip().lower()
        for address in settings.AUTHORIZED_LOGIN_EMAILS
    }

    if email not in allowed_emails:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Localiza a conta existente no Django.
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()

    if user is None or not user.is_active:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Consome o código antes de iniciar a sessão.
    login_code.used_at = now
    login_code.save(update_fields=["used_at"])

    # Identifica os administradores autorizados.
    admin_emails = {
        address.strip().lower()
        for address in settings.ADMIN_EMAILS
    }
    is_admin = email in admin_emails

    # Cria ou atualiza o perfil do usuário.
    profile, _ = UserProfile.objects.get_or_create(user=user)

    profile.role = (
        UserProfile.Role.ADMIN
        if is_admin
        else UserProfile.Role.STUDENT
    )
    profile.save(update_fields=["role"])

    # Sincroniza as permissões administrativas.
    if user.is_staff != is_admin or user.is_superuser != is_admin:
        user.is_staff = is_admin
        user.is_superuser = is_admin
        user.save(update_fields=["is_staff", "is_superuser"])

    # Inicia a sessão autenticada do Django.
    login(request, user)

    return JsonResponse({
        "message": "Login realizado com sucesso."
    })
