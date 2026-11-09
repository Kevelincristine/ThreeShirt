
"""
Django settings for backend project.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


# Build paths inside the project.
BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega as variáveis do arquivo .env.
load_dotenv(BASE_DIR / ".env")


# E-mails autorizados a administrar o ThreeShirt.
ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.getenv("THREESHIRT_ADMIN_EMAILS", "").split(",")
    if email.strip()
]

# E-mails autorizados a entrar no ThreeShirt.
AUTHORIZED_LOGIN_EMAILS = [
    email.strip().lower()
    for email in os.getenv("THREESHIRT_ALLOWED_EMAILS", "").split(",")
    if email.strip()
]


# Segurança.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
DEBUG = os.getenv("DJANGO_DEBUG", "True").strip().lower() == "true"
ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "192.168.0.95",
]

_default_csrf_trusted_origins = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://192.168.0.95:5173",
]
_csrf_trusted_origins_setting = os.getenv(
    "THREESHIRT_CSRF_TRUSTED_ORIGINS",
    ",".join(_default_csrf_trusted_origins),
).strip()
CSRF_TRUSTED_ORIGINS = _default_csrf_trusted_origins
if _csrf_trusted_origins_setting:
    _csrf_trusted_origins = [
        origin.strip()
        for origin in _csrf_trusted_origins_setting.split(",")
    ]
    if any(not origin for origin in _csrf_trusted_origins):
        raise ImproperlyConfigured(
            "THREESHIRT_CSRF_TRUSTED_ORIGINS must contain only valid origins."
        )

    for _origin in _csrf_trusted_origins:
        try:
            _parsed_origin = urlsplit(_origin)
            _parsed_origin.port
        except ValueError as exc:
            raise ImproperlyConfigured(
                "THREESHIRT_CSRF_TRUSTED_ORIGINS must contain only valid origins."
            ) from exc

        if (
            _parsed_origin.scheme not in {"http", "https"}
            or not _parsed_origin.hostname
            or "*" in _parsed_origin.netloc
            or _parsed_origin.username is not None
            or _parsed_origin.password is not None
            or _parsed_origin.path
            or _parsed_origin.query
            or _parsed_origin.fragment
            or any(character.isspace() for character in _origin)
            or _parsed_origin.netloc.endswith(":")
            or _origin != f"{_parsed_origin.scheme}://{_parsed_origin.netloc}"
        ):
            raise ImproperlyConfigured(
                "THREESHIRT_CSRF_TRUSTED_ORIGINS must contain only valid origins."
            )

    CSRF_TRUSTED_ORIGINS = _csrf_trusted_origins


# Aplicações.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "election",
]


# Middleware.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.CredentialRevisionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# URLs e templates.
ROOT_URLCONF = "backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "backend.wsgi.application"


# Banco de dados.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL:
    import dj_database_url

    DATABASES = {
        "default": dj_database_url.parse(DATABASE_URL),
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "transaction_mode": "IMMEDIATE",
            },
        }
    }


# Validação de senhas.
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# Internacionalização.
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True


# Arquivos estáticos e imagens enviadas.
STATIC_URL = "static/"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"

# Tipo padrão de chave primária.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
