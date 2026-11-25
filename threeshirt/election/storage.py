import json
import logging
import re
from io import BytesIO, TextIOWrapper
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import FileSystemStorage


logger = logging.getLogger(__name__)


class SupabaseStorageError(OSError):
    pass


class PrivateProposalStorage(FileSystemStorage):
    _MAX_ERROR_BODY_BYTES = 4096
    _MAX_LOG_VALUE_LENGTH = 512
    _REDACTED = "[REDACTED]"
    _BEARER_TOKEN_PATTERN = re.compile(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"
    )
    _SECRET_ASSIGNMENT_PATTERN = re.compile(
        r"(?i)\b(authorization|api[_-]?key|access[_-]?token|"
        r"refresh[_-]?token|service[_-]?role[_-]?key|token|secret|password)"
        r"(\s*[:=]\s*)[^\s,;]+"
    )
    _JWT_PATTERN = re.compile(
        r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    )
    _SUPABASE_KEY_PATTERN = re.compile(
        r"\bsb_(?:secret|publishable)_[A-Za-z0-9_-]+\b"
    )

    def __init__(self, location=None, base_url=None, prefix=""):
        self.prefix = self._normalize_prefix(prefix)
        super().__init__(location=location, base_url=base_url)

    @property
    def base_location(self):
        if self._location is not None:
            return self._location
        location = settings.PRIVATE_MEDIA_ROOT
        if self.prefix:
            return Path(location).joinpath(*self.prefix.split("/"))
        return location

    @property
    def location(self):
        return str(self.base_location)

    @staticmethod
    def _normalize_prefix(prefix):
        normalized = str(prefix).replace("\\", "/").strip("/")
        if normalized and any(
            part in {"", ".", ".."} for part in normalized.split("/")
        ):
            raise ValueError("Storage prefix must be a relative path.")
        return normalized

    @property
    def uses_supabase(self):
        return settings.SUPABASE_STORAGE_ENABLED

    def _object_key(self, name):
        normalized = str(name).replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or any(not part or part in {".", ".."} for part in parts)
        ):
            raise ValueError("Storage name must be a relative path.")
        return "/".join(part for part in (self.prefix, normalized) if part)

    def _object_url(self, name, info=False):
        endpoint = "info/" if info else ""
        bucket = quote(settings.SUPABASE_STORAGE_BUCKET, safe="")
        key = quote(self._object_key(name), safe="/")
        return (
            f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/"
            f"{endpoint}{bucket}/{key}"
        )

    def _request(
        self,
        method,
        url,
        *,
        data=None,
        content_type=None,
        allow_not_found=False,
        allow_missing_object=False,
        operation="request",
        extra_headers=None,
    ):
        key = settings.SUPABASE_SERVICE_ROLE_KEY
        headers = {
            "Authorization": f"Bearer {key}",
            "apikey": key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except HTTPError as exc:
            status = exc.code
            try:
                error_content = exc.read(self._MAX_ERROR_BODY_BYTES + 1)
            except OSError:
                error_content = None
            finally:
                exc.close()

            missing_object = (
                allow_missing_object
                and status in {400, 404}
                and error_content is not None
                and len(error_content) <= self._MAX_ERROR_BODY_BYTES
                and self._is_missing_object_response(status, error_content)
            )
            allowed_not_found = allow_not_found and status == 404
            error_code, error_message = self._error_details(error_content)
            log = (
                logger.warning
                if status == 400 or not (missing_object or allowed_not_found)
                else logger.debug
            )
            log(
                "Supabase Storage HTTP error: method=%s operation=%s "
                "status=%s code=%s message=%s",
                method,
                operation,
                status,
                error_code or "-",
                error_message or "-",
            )

            if missing_object:
                return status, b""
            if allowed_not_found:
                return status, b""
            raise SupabaseStorageError(
                f"Supabase Storage returned HTTP {status}."
            ) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseStorageError(
                "Unable to communicate with Supabase Storage."
            ) from None

    @classmethod
    def _error_details(cls, content):
        if content is None or len(content) > cls._MAX_ERROR_BODY_BYTES:
            return None, None
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        return (
            cls._sanitize_log_value(payload.get("code")),
            cls._sanitize_log_value(payload.get("message")),
        )

    @classmethod
    def _sanitize_log_value(cls, value):
        if not isinstance(value, str):
            return None
        service_role_key = getattr(settings, "SUPABASE_SERVICE_ROLE_KEY", "")
        if service_role_key:
            value = value.replace(service_role_key, cls._REDACTED)
        value = cls._BEARER_TOKEN_PATTERN.sub(
            f"Bearer {cls._REDACTED}",
            value,
        )
        value = cls._SECRET_ASSIGNMENT_PATTERN.sub(
            rf"\1\2{cls._REDACTED}",
            value,
        )
        value = cls._JWT_PATTERN.sub(cls._REDACTED, value)
        value = cls._SUPABASE_KEY_PATTERN.sub(cls._REDACTED, value)
        value = re.sub(r"[\r\n\t]+", " ", value)
        return value[:cls._MAX_LOG_VALUE_LENGTH]

    @staticmethod
    def _is_missing_object_response(status, content):
        if not content:
            return False
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        return status in {400, 404} and payload.get("code") == "NoSuchKey"

    def _open(self, name, mode="rb"):
        if not self.uses_supabase:
            return super()._open(name, mode)
        if mode not in {"rb", "r"}:
            raise ValueError("Private image storage supports read-only opens.")
        status, content = self._request(
            "GET",
            self._object_url(name),
            allow_not_found=True,
            operation="open",
        )
        if status == 404:
            raise FileNotFoundError(name)
        file_content = BytesIO(content)
        if mode == "r":
            file_content = TextIOWrapper(file_content)
        return File(file_content, name=name)

    def _save(self, name, content):
        if not self.uses_supabase:
            return super()._save(name, content)

        if hasattr(content, "seek"):
            content.seek(0)
        data = content.read()
        content_type = getattr(content, "content_type", None)
        self._request(
            "POST",
            self._object_url(name),
            data=data,
            content_type=content_type or "application/octet-stream",
            operation="upload",
            extra_headers={"x-upsert": "false"},
        )
        return name

    def delete(self, name):
        if not self.uses_supabase:
            return super().delete(name)
        payload = json.dumps({"prefixes": [self._object_key(name)]}).encode()
        self._request(
            "DELETE",
            (
                f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/"
                f"{quote(settings.SUPABASE_STORAGE_BUCKET, safe='')}"
            ),
            data=payload,
            content_type="application/json",
            allow_not_found=True,
            operation="delete",
        )

    def exists(self, name):
        if not self.uses_supabase:
            return super().exists(name)
        status, _content = self._request(
            "GET",
            self._object_url(name, info=True),
            allow_missing_object=True,
            operation="exists",
        )
        if status in {400, 404}:
            return False
        if not 200 <= status < 300:
            raise SupabaseStorageError(
                f"Supabase Storage returned HTTP {status}."
            )
        return True

    def size(self, name):
        if not self.uses_supabase:
            return super().size(name)
        _status, content = self._request(
            "GET",
            self._object_url(name, info=True),
            operation="size",
        )
        try:
            response_metadata = json.loads(content)
        except (TypeError, ValueError):
            raise SupabaseStorageError(
                "Supabase Storage returned invalid object metadata."
            ) from None

        size = (
            response_metadata.get("size")
            if isinstance(response_metadata, dict)
            else None
        )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SupabaseStorageError(
                "Supabase Storage returned invalid object metadata."
            ) from None
        return size

    def listdir(self, path):
        if not self.uses_supabase:
            return super().listdir(path)

        prefix = self._object_key(path) if path else self.prefix
        endpoint = (
            f"{settings.SUPABASE_URL.rstrip('/')}/storage/v1/object/list/"
            f"{quote(settings.SUPABASE_STORAGE_BUCKET, safe='')}"
        )
        files = set()
        directories = set()
        offset = 0
        while True:
            payload = json.dumps({
                "prefix": prefix,
                "limit": 1000,
                "offset": offset,
            }).encode()
            _status, content = self._request(
                "POST",
                endpoint,
                data=payload,
                content_type="application/json",
                operation="listdir",
            )
            try:
                entries = json.loads(content)
            except json.JSONDecodeError:
                raise SupabaseStorageError(
                    "Supabase Storage returned invalid listing metadata."
                ) from None
            if not isinstance(entries, list):
                raise SupabaseStorageError(
                    "Supabase Storage returned invalid listing metadata."
                )
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("name"), str
                ):
                    continue
                remainder = entry["name"]
                if prefix:
                    remainder = remainder.removeprefix(prefix).lstrip("/")
                if "/" in remainder:
                    directories.add(remainder.split("/", 1)[0])
                elif remainder:
                    files.add(remainder)
            if len(entries) < 1000:
                break
            offset += len(entries)
        return sorted(directories), sorted(files)

    def path(self, name):
        if self.uses_supabase:
            raise NotImplementedError(
                "Supabase Storage objects do not have local filesystem paths."
            )
        return super().path(name)

    def url(self, name):
        raise ValueError("Propostas privadas não possuem URL pública.")
