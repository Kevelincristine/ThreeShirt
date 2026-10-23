
from django.urls import path

from . import views

urlpatterns = [
    path(
        "csrf/",
        views.initialize_csrf,
        name="initialize_csrf",
    ),
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
