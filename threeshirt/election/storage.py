import json
from io import BytesIO, TextIOWrapper
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import FileSystemStorage


class SupabaseStorageError(OSError):
    pass


class PrivateProposalStorage(FileSystemStorage):
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
            exc.close()
            if allow_not_found and exc.code == 404:
                return exc.code, b""
            raise SupabaseStorageError(
                f"Supabase Storage returned HTTP {exc.code}."
            ) from None
        except (URLError, TimeoutError, OSError):
            raise SupabaseStorageError(
                "Unable to communicate with Supabase Storage."
            ) from None

    def _open(self, name, mode="rb"):
        if not self.uses_supabase:
            return super()._open(name, mode)
        if mode not in {"rb", "r"}:
            raise ValueError("Private image storage supports read-only opens.")
        status, content = self._request(
            "GET",
            self._object_url(name),
            allow_not_found=True,
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
        )

    def exists(self, name):
        if not self.uses_supabase:
            return super().exists(name)
        status, _content = self._request(
            "GET",
            self._object_url(name, info=True),
            allow_not_found=True,
        )
        return status != 404

    def size(self, name):
        if not self.uses_supabase:
            return super().size(name)
        _status, content = self._request(
            "GET",
            self._object_url(name, info=True),
        )
        try:
            metadata = json.loads(content)
            return int(metadata["metadata"]["size"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SupabaseStorageError(
                "Supabase Storage returned invalid object metadata."
            ) from None

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
