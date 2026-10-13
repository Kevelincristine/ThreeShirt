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
    """
    Solicita um código temporário de acesso.

    O endpoint aceita somente requisições POST.
    Para evitar revelar quais e-mails são autorizados,
    a resposta pública será a mesma nos dois casos.
    """

    # Lê o endereço enviado pelo frontend.
    email = request.POST.get("email", "").strip().lower()

    # Resposta genérica para não revelar se o e-mail é autorizado.
    response = {
        "message": (
            "Se o endereço estiver autorizado, "
            "você receberá instruções para continuar."
        )
    }

    # Valida o formato básico do endereço.
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse(response, status=200)

    # Só continua se o endereço estiver na lista autorizada.
    allowed_emails = settings.AUTHORIZED_LOGIN_EMAILS

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

    # Armazena uma versão protegida do código, nunca o código puro.
    code_hash = make_password(code)
    now = timezone.now()

    # Invalida códigos anteriores ainda não utilizados.
    EmailLoginCode.objects.filter(
        email=email,
        used_at__isnull=True,
    ).update(used_at=now)

    # Guarda o novo código com validade de dez minutos.
    EmailLoginCode.objects.create(
        email=email,
        code_hash=code_hash,
        expires_at=now + timedelta(minutes=10),
    )

    # Envia a mensagem usando a configuração de e-mail do Django.
    send_mail(
        subject="Seu código de acesso ao ThreeShirt",
        message=(
            f"Seu código de acesso é: {code}\n\n"
            "Ele expira em 10 minutos. "
            "Se você não solicitou este código, ignore esta mensagem."
        ),
        from_email="noreply@threeshirt.local",
        recipient_list=[email],
        fail_silently=False,
    )

    return JsonResponse(response, status=200)
@require_POST
def verify_login_code(request):
    """Valida o código temporário e inicia a sessão do usuário."""

    email = request.POST.get("email", "").strip().lower()
    code = request.POST.get("code", "").strip()

    # Usamos uma resposta genérica para evitar revelar contas existentes.
    invalid_response = {
        "error": "Código inválido ou expirado."
    }

    # Valida o formato básico do e-mail.
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse(invalid_response, status=400)

    # Um código de acesso deve ter exatamente seis dígitos.
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return JsonResponse(invalid_response, status=400)

    # Busca o código mais recente que ainda não foi utilizado.
    login_code = (
        EmailLoginCode.objects
        .filter(email=email, used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )

    if login_code is None:
        return JsonResponse(invalid_response, status=400)

    # Rejeita códigos expirados.
    now = timezone.now()

    if login_code.expires_at <= now:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Limita a cinco tentativas para o mesmo código.
    if login_code.attempts >= 5:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Registra cada tentativa antes de verificar o código.
    login_code.attempts += 1
    login_code.save(update_fields=["attempts"])

    if not check_password(code, login_code.code_hash):
        return JsonResponse(invalid_response, status=400)

    # Confirma que o endereço continua autorizado.
    if email not in settings.AUTHORIZED_LOGIN_EMAILS:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Localiza a conta correspondente no Django.
    User = get_user_model()

    user = User.objects.filter(email__iexact=email).first()

    # Não criamos contas automaticamente para e-mails desconhecidos.
    if user is None or not user.is_active:
        login_code.used_at = now
        login_code.save(update_fields=["used_at"])
        return JsonResponse(invalid_response, status=400)

    # Consome o código antes de iniciar a sessão.
    login_code.used_at = now
    login_code.save(update_fields=["used_at"])

    # Inicia a sessão autenticada do Django.
    login(request, user)

    return JsonResponse({
        "message": "Login realizado com sucesso."
    })