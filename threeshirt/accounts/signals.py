from django.contrib.auth.signals import user_logged_in

from .models import UserProfile

SESSION_CREDENTIAL_REVISION_KEY = "threeshirt_credential_revision"


def store_credential_revision(sender, request, user, **kwargs):
    try:
        revision = user.profile.credential_revision
    except UserProfile.DoesNotExist:
        revision = 0
    request.session[SESSION_CREDENTIAL_REVISION_KEY] = revision


user_logged_in.connect(
    store_credential_revision,
    dispatch_uid="accounts.store_credential_revision",
)
