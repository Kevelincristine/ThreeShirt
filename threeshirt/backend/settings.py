
"""
Django settings for backend project.
"""

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


# Build paths inside the project.
BASE_DIR = Path(__file__).resolve().parent.parent

# Carrega as variáveis do arquivo .env.
load_dotenv(BASE_DIR / ".env")


def _is_test_process():
    if os.getenv("THREESHIRT_TESTING", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True

    executable = Path(sys.argv[0]).name.lower()
    if executable.startswith(("pytest", "py.test")):
        return True
    if executable == "-m" and len(sys.argv) > 1:
        return sys.argv[1] in {"pytest", "unittest"}
    return len(sys.argv) > 1 and sys.argv[1] == "test"


TESTING = _is_test_process()
_TEST_RUNTIME_DIR = (
    tempfile.TemporaryDirectory(prefix="threeshirt-test-")
    if TESTING
    else None
)


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
DJANGO_ENV = os.getenv("DJANGO_ENV", "development").strip().lower()
if DJANGO_ENV not in {"development", "production"}:
    raise ImproperlyConfigured(
        "DJANGO_ENV must be either development or production."
    )
_IS_PRODUCTION = DJANGO_ENV == "production" and not TESTING


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value.")


_debug_default = _env_bool("DEBUG", not _IS_PRODUCTION)
DEBUG = _env_bool("DJANGO_DEBUG", _debug_default)
if _IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production.")

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    os.getenv("SECRET_KEY", ""),
).strip()
if _IS_PRODUCTION and (
    len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 16
):
    raise ImproperlyConfigured(
        "A strong DJANGO_SECRET_KEY must be configured in production."
    )

_local_allowed_hosts = ["127.0.0.1", "localhost", "192.168.0.95"]
_allowed_hosts_setting = os.getenv("ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = (
    [host.strip() for host in _allowed_hosts_setting.split(",") if host.strip()]
    if _allowed_hosts_setting
    else ([] if _IS_PRODUCTION else _local_allowed_hosts)
)
if _IS_PRODUCTION and (
    not ALLOWED_HOSTS
    or any("*" in host or host.startswith(".") for host in ALLOWED_HOSTS)
):
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS must contain explicit hosts in production."
    )

_local_frontend_origins = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://192.168.0.95:5173",
]


def _configured_origins(name, default):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default.copy()
    return [origin.strip() for origin in value.split(",")]


def _validate_origins(origins, name):
    if any(not origin for origin in origins):
        raise ImproperlyConfigured(
            f"{name} must contain only valid origins."
        )

    for origin in origins:
        try:
            parsed_origin = urlsplit(origin)
            parsed_origin.port
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"{name} must contain only valid origins."
            ) from exc

        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.hostname
            or "*" in parsed_origin.netloc
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or any(character.isspace() for character in origin)
            or parsed_origin.netloc.endswith(":")
            or origin != f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        ):
            raise ImproperlyConfigured(
                f"{name} must contain only valid origins."
            )


_csrf_trusted_origins_setting = os.getenv(
    "CSRF_TRUSTED_ORIGINS",
    os.getenv("THREESHIRT_CSRF_TRUSTED_ORIGINS"),
)
_default_csrf_trusted_origins = (
    [] if _IS_PRODUCTION else _local_frontend_origins
)
if (
    _csrf_trusted_origins_setting is None
    or not _csrf_trusted_origins_setting.strip()
):
    CSRF_TRUSTED_ORIGINS = _default_csrf_trusted_origins.copy()
else:
    CSRF_TRUSTED_ORIGINS = [
        origin.strip() for origin in _csrf_trusted_origins_setting.split(",")
    ]
_validate_origins(CSRF_TRUSTED_ORIGINS, "CSRF_TRUSTED_ORIGINS")

CORS_ALLOWED_ORIGINS = _configured_origins(
    "CORS_ALLOWED_ORIGINS",
    [] if _IS_PRODUCTION else _local_frontend_origins,
)
_validate_origins(CORS_ALLOWED_ORIGINS, "CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True

if _IS_PRODUCTION:
    if not CSRF_TRUSTED_ORIGINS or not CORS_ALLOWED_ORIGINS:
        raise ImproperlyConfigured(
            "CSRF_TRUSTED_ORIGINS and CORS_ALLOWED_ORIGINS must be configured in production."
        )
    if any(
        urlsplit(origin).scheme != "https"
        for origin in CSRF_TRUSTED_ORIGINS + CORS_ALLOWED_ORIGINS
    ):
        raise ImproperlyConfigured(
            "Production CORS and CSRF origins must use HTTPS."
        )
    if not set(CORS_ALLOWED_ORIGINS).issubset(CSRF_TRUSTED_ORIGINS):
        raise ImproperlyConfigured(
            "Every CORS_ALLOWED_ORIGINS entry must also be CSRF trusted."
        )


def _same_site_setting(name, default):
    value = os.getenv(name, default).strip().lower()
    choices = {"lax": "Lax", "strict": "Strict", "none": "None"}
    if value not in choices:
        raise ImproperlyConfigured(
            f"{name} must be Lax, Strict, or None."
        )
    return choices[value]


SESSION_COOKIE_SECURE = _env_bool(
    "SESSION_COOKIE_SECURE",
    _IS_PRODUCTION or not DEBUG,
)
CSRF_COOKIE_SECURE = _env_bool(
    "CSRF_COOKIE_SECURE",
    _IS_PRODUCTION or not DEBUG,
)
SESSION_COOKIE_SAMESITE = _same_site_setting(
    "SESSION_COOKIE_SAMESITE",
    "None" if _IS_PRODUCTION else "Lax",
)
CSRF_COOKIE_SAMESITE = _same_site_setting(
    "CSRF_COOKIE_SAMESITE",
    "None" if _IS_PRODUCTION else "Lax",
)
if _IS_PRODUCTION and (
    not SESSION_COOKIE_SECURE or not CSRF_COOKIE_SECURE
):
    raise ImproperlyConfigured("Cookies must be Secure in production.")
if (
    SESSION_COOKIE_SAMESITE == "None"
    and not SESSION_COOKIE_SECURE
) or (
    CSRF_COOKIE_SAMESITE == "None"
    and not CSRF_COOKIE_SECURE
):
    raise ImproperlyConfigured("SameSite=None cookies must also be Secure.")

_proxy_ssl_header_setting = os.getenv("SECURE_PROXY_SSL_HEADER")
if _proxy_ssl_header_setting is None:
    SECURE_PROXY_SSL_HEADER = (
        ("HTTP_X_FORWARDED_PROTO", "https") if _IS_PRODUCTION else None
    )
elif not _proxy_ssl_header_setting.strip():
    SECURE_PROXY_SSL_HEADER = None
else:
    _proxy_ssl_header_parts = [
        part.strip() for part in _proxy_ssl_header_setting.split(",")
    ]
    if len(_proxy_ssl_header_parts) != 2 or not all(
        _proxy_ssl_header_parts
    ):
        raise ImproperlyConfigured(
            "SECURE_PROXY_SSL_HEADER must contain a header and value separated by a comma."
        )
    SECURE_PROXY_SSL_HEADER = tuple(_proxy_ssl_header_parts)

SECURE_SSL_REDIRECT = _env_bool(
    "SECURE_SSL_REDIRECT",
    _IS_PRODUCTION,
)
if _IS_PRODUCTION and SECURE_PROXY_SSL_HEADER is None:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if _IS_PRODUCTION and not SECURE_SSL_REDIRECT:
    raise ImproperlyConfigured(
        "SECURE_SSL_REDIRECT must be enabled in production."
    )

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY",
    "",
).strip()
SUPABASE_STORAGE_BUCKET = os.getenv(
    "SUPABASE_STORAGE_BUCKET",
    "",
).strip()
_supabase_storage_values = (
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_STORAGE_BUCKET,
)
_storage_backend = os.getenv(
    "DJANGO_STORAGE",
    "supabase" if _IS_PRODUCTION else "local",
).strip().lower()
if TESTING:
    _storage_backend = "local"
if _storage_backend not in {"local", "supabase"}:
    raise ImproperlyConfigured(
        "DJANGO_STORAGE must be either local or supabase."
    )
DJANGO_STORAGE = _storage_backend
if _storage_backend == "supabase" and not all(_supabase_storage_values):
    raise ImproperlyConfigured(
        "SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and "
        "SUPABASE_STORAGE_BUCKET must be configured when DJANGO_STORAGE=supabase."
    )
SUPABASE_STORAGE_ENABLED = _storage_backend == "supabase"
if SUPABASE_STORAGE_ENABLED:
    _supabase_url = urlsplit(SUPABASE_URL)
    if (
        _supabase_url.scheme != "https"
        or not _supabase_url.hostname
        or _supabase_url.username is not None
        or _supabase_url.password is not None
        or _supabase_url.path
        or _supabase_url.query
        or _supabase_url.fragment
    ):
        raise ImproperlyConfigured(
            "SUPABASE_URL must be an HTTPS project origin."
        )
if _IS_PRODUCTION and SUPABASE_STORAGE_BUCKET != "ThreeShirt":
    raise ImproperlyConfigured(
        "SUPABASE_STORAGE_BUCKET must be ThreeShirt in production."
    )
if _IS_PRODUCTION and not SUPABASE_STORAGE_ENABLED:
    raise ImproperlyConfigured(
        "Supabase private storage requires SUPABASE_URL, "
        "SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_STORAGE_BUCKET in production."
    )


# Aplicações.
INSTALLED_APPS = [
    "corsheaders",
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
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.middleware.CredentialRevisionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SESSION_COOKIE_HTTPONLY = True
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
if TESTING:
    _test_database_backend = os.getenv(
        "THREESHIRT_TEST_DATABASE",
        "sqlite",
    ).strip().lower()
    if _test_database_backend not in {"sqlite", "postgresql"}:
        raise ImproperlyConfigured(
            "THREESHIRT_TEST_DATABASE must be sqlite or postgresql."
        )
    if _test_database_backend == "postgresql":
        try:
            _test_database_url = urlsplit(DATABASE_URL)
            _test_database_port = _test_database_url.port
        except ValueError as exc:
            raise ImproperlyConfigured(
                "PostgreSQL tests require the dedicated local test database."
            ) from exc
        if (
            _test_database_url.scheme not in {"postgres", "postgresql"}
            or _test_database_url.hostname
            not in {"127.0.0.1", "::1", "localhost"}
            or _test_database_port != 55432
            or _test_database_url.path.strip("/")
            != "threeshirt_test_local"
        ):
            raise ImproperlyConfigured(
                "PostgreSQL tests require the dedicated local test database."
            )
    _database_backend = _test_database_backend
else:
    _database_backend = os.getenv(
        "DJANGO_DATABASE",
        "postgresql" if _IS_PRODUCTION else "sqlite",
    ).strip().lower()
if _database_backend not in {"sqlite", "postgresql"}:
    raise ImproperlyConfigured(
        "DJANGO_DATABASE must be either sqlite or postgresql."
    )
if _IS_PRODUCTION and _database_backend != "postgresql":
    raise ImproperlyConfigured(
        "Production requires DJANGO_DATABASE=postgresql."
    )

if _database_backend == "postgresql":
    if not DATABASE_URL:
        raise ImproperlyConfigured(
            "DATABASE_URL must be configured when PostgreSQL is enabled."
        )

    try:
        database_url = urlsplit(DATABASE_URL)
        database_url.port
    except ValueError as exc:
        raise ImproperlyConfigured(
            "DATABASE_URL is not a valid PostgreSQL URL."
        ) from exc

    if (
        database_url.scheme not in {"postgres", "postgresql"}
        or not database_url.hostname
        or not database_url.path.strip("/")
    ):
        raise ImproperlyConfigured(
            "DATABASE_URL must be a PostgreSQL URL with a host and database name."
        )

    allowed_ssl_modes = {"require", "verify-ca", "verify-full"}
    if TESTING and _database_backend == "postgresql":
        allowed_ssl_modes.add("disable")
    default_sslmode = (
        "disable"
        if TESTING and _database_backend == "postgresql"
        else "require"
    )
    sslmode = os.getenv(
        "DATABASE_SSLMODE",
        default_sslmode,
    ).strip().lower()
    if sslmode not in allowed_ssl_modes:
        raise ImproperlyConfigured(
            "DATABASE_SSLMODE must use an allowed SSL mode for this environment."
        )

    try:
        connection_max_age = int(
            os.getenv(
                "DATABASE_CONN_MAX_AGE",
                "0",
            ).strip()
        )
    except ValueError as exc:
        raise ImproperlyConfigured(
            "DATABASE_CONN_MAX_AGE must be a non-negative integer."
        ) from exc
    if connection_max_age < 0:
        raise ImproperlyConfigured(
            "DATABASE_CONN_MAX_AGE must be a non-negative integer."
        )

    disable_server_side_cursors = _env_bool(
        "DATABASE_DISABLE_SERVER_SIDE_CURSORS",
        True,
    )
    import dj_database_url

    try:
        postgres_database = dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=connection_max_age,
            conn_health_checks=True,
            disable_server_side_cursors=disable_server_side_cursors,
            ssl_require=True,
        )
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            "DATABASE_URL is not a valid PostgreSQL URL."
        ) from exc

    if postgres_database["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured(
            "DATABASE_URL must use PostgreSQL when PostgreSQL is enabled."
        )
    postgres_database.setdefault("OPTIONS", {})["sslmode"] = sslmode
    DATABASES = {"default": postgres_database}
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

if TESTING and _database_backend == "sqlite":
    _test_database_name = Path(_TEST_RUNTIME_DIR.name) / "test.sqlite3"
    DATABASES["default"]["NAME"] = _test_database_name
    DATABASES["default"]["TEST"] = {"NAME": _test_database_name}
elif TESTING:
    DATABASES["default"]["TEST"] = {
        "NAME": "test_threeshirt_test_local",
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
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
PRIVATE_MEDIA_ROOT = BASE_DIR / "private_media"
if TESTING:
    MEDIA_ROOT = Path(_TEST_RUNTIME_DIR.name) / "media"
    PRIVATE_MEDIA_ROOT = Path(_TEST_RUNTIME_DIR.name) / "private_media"
MAINTENANCE_BACKUP_DIR = Path(
    os.getenv(
        "MAINTENANCE_BACKUP_DIR",
        BASE_DIR / "backups" / "maintenance",
    )
)
if TESTING:
    MAINTENANCE_BACKUP_DIR = Path(_TEST_RUNTIME_DIR.name) / "backups"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Tipo padrão de chave primária.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
