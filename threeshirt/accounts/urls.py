
from django.urls import path

from . import views

urlpatterns = [
    path(
        "csrf/",
        views.initialize_csrf,
        name="initialize_csrf",
    ),
    path(
        "session/",
        views.session_status,
        name="session_status",
    ),
    path(
        "logout/",
        views.logout_user,
        name="logout",
    ),
    path(
        "login/",
        views.login_with_password,
        name="login",
    ),
    path("first-access/", views.first_access, name="first_access"),
    path(
        "password/change/",
        views.change_password,
        name="change_password",
    ),
]
