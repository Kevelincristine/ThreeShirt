
from django.urls import path

from . import views

urlpatterns = [
    # Solicita um código temporário por e-mail.
    path(
        "request-code/",
        views.request_login_code,
        name="request_login_code",
    ),

    # Verifica o código e inicia a sessão.
    path(
        "verify-code/",
        views.verify_login_code,
        name="verify_login_code",
    ),
]
