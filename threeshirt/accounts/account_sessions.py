from django.contrib.sessions.models import Session
from django.utils import timezone


SESSION_USER_KEY = "_auth_user_id"


def revoke_user_sessions(user_id, keep_session_key=None):
    keys = []
    now = timezone.now()
    for session in Session.objects.filter(expire_date__gt=now).iterator():
        if session.session_key == keep_session_key:
            continue
        decoded = session.get_decoded()
        if decoded.get(SESSION_USER_KEY) == str(user_id):
            keys.append(session.session_key)
    if keys:
        Session.objects.filter(session_key__in=keys).delete()
    return len(keys)
