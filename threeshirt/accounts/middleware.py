from django.contrib.auth import logout

from .access import is_authorized_session_user
from .models import UserProfile
from .signals import SESSION_CREDENTIAL_REVISION_KEY


class CredentialRevisionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            try:
                has_admin_permissions = (
                    user.profile.role in UserProfile.ADMIN_ROLES
                )
            except UserProfile.DoesNotExist:
                has_admin_permissions = False
            if (
                has_admin_permissions
                and not is_authorized_session_user(user)
            ):
                logout(request)
                return self.get_response(request)
            try:
                current_revision = user.profile.credential_revision
            except UserProfile.DoesNotExist:
                current_revision = 0
            session_revision = request.session.get(
                SESSION_CREDENTIAL_REVISION_KEY
            )
            if session_revision != current_revision:
                logout(request)
        return self.get_response(request)
