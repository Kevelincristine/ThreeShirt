from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .models import UserProfile


def normalize_authorized_email(value):
    if not isinstance(value, str):
        return None
    email = value.strip().casefold()
    if not email or len(email) > 254:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email


def normalize_gmail_email(value):
    email = normalize_authorized_email(value)
    if email is None:
        return None
    if email.rsplit("@", 1)[-1] != "gmail.com":
        return None
    return email


def _configured_emails(setting_name):
    return {
        email.strip().casefold()
        for email in getattr(settings, setting_name, [])
        if isinstance(email, str) and email.strip()
    }


def is_authorized_login_email(email):
    normalized = normalize_authorized_email(email)
    return normalized in _configured_emails("AUTHORIZED_LOGIN_EMAILS")


def is_authorized_admin_email(email):
    normalized = normalize_authorized_email(email)
    return normalized in _configured_emails("ADMIN_EMAILS")


def is_authorized_session_user(user):
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False

    email = (user.email or "").strip().casefold()
    if not email:
        return False
    try:
        role = user.profile.role
    except UserProfile.DoesNotExist:
        return False

    if (
        role == UserProfile.Role.ADMIN
        and is_authorized_admin_email(email)
    ):
        return True
    return is_authorized_login_email(email)
