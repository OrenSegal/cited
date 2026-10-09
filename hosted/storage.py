"""Where hosted certificates live. Standard library only.

Two backends behind one small interface:

- LocalStorage: a directory on disk. The default, for local runs and tests.
- SupabaseStorage: a Supabase Storage bucket over its REST API. Selected by
  `storage_from_env` when SUPABASE_URL, SUPABASE_SERVICE_KEY and
  SUPABASE_BUCKET are all set. Required on Vercel, whose disk does not
  outlive a request.

Keys are short relative paths ("certs/AbC123.html", "codes/<sha256>") built
by the app, never taken from a request without validation.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

__all__ = ["LocalStorage", "Storage", "StorageError", "SupabaseStorage", "storage_from_env"]

SUPABASE_TIMEOUT = 15.0


class StorageError(Exception):
    """Storage is misconfigured or unreachable."""


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def create(self, key: str, data: bytes) -> bool:
        """Write `key` only if it does not exist yet. True if this call created it."""
        ...


def _check_key(key: str) -> str:
    parts = key.split("/")
    if key.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"bad storage key {key!r}")
    return key


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def create(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:  # atomic: fails if the file exists
                handle.write(data)
        except FileExistsError:
            return False
        return True


class SupabaseStorage:
    """Supabase Storage REST API, with the service role key. Use a private
    bucket: certificates are served through the app, not from Supabase."""

    def __init__(self, url: str, service_key: str, bucket: str,
                 opener: urllib.request.OpenerDirector | None = None) -> None:
        self.base = url.rstrip("/") + "/storage/v1/object/" + urllib.parse.quote(bucket, safe="")
        self.service_key = service_key
        self._open = (opener or urllib.request.build_opener()).open

    def _request(self, method: str, key: str, data: bytes | None = None,
                 headers: dict[str, str] | None = None) -> urllib.request.Request:
        url = f"{self.base}/{urllib.parse.quote(_check_key(key))}"
        return urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.service_key}",
            "apikey": self.service_key,
            **(headers or {}),
        })

    def _upload(self, key: str, data: bytes, content_type: str, upsert: bool) -> int:
        request = self._request("POST", key, data, {
            "Content-Type": content_type,
            "x-upsert": "true" if upsert else "false",
        })
        try:
            with self._open(request, timeout=SUPABASE_TIMEOUT) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code
        except (urllib.error.URLError, OSError) as exc:
            raise StorageError(f"Supabase Storage unreachable: {exc}") from None

    def put(self, key: str, data: bytes, content_type: str) -> None:
        status = self._upload(key, data, content_type, upsert=True)
        if status >= 300:
            raise StorageError(f"Supabase Storage refused the upload (HTTP {status})")

    def get(self, key: str) -> bytes | None:
        try:
            with self._open(self._request("GET", key), timeout=SUPABASE_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            # Supabase answers a missing object with 400 or 404, depending on version.
            if exc.code in (400, 404):
                return None
            raise StorageError(f"Supabase Storage read failed (HTTP {exc.code})") from None
        except (urllib.error.URLError, OSError) as exc:
            raise StorageError(f"Supabase Storage unreachable: {exc}") from None

    def create(self, key: str, data: bytes) -> bool:
        status = self._upload(key, data, "application/octet-stream", upsert=False)
        if status < 300:
            return True
        # An existing object without upsert comes back as 409, or 400 with a
        # "Duplicate" body on older versions.
        if status in (400, 409):
            return False
        raise StorageError(f"Supabase Storage refused the upload (HTTP {status})")


def storage_from_env(env: dict[str, str] | None = None) -> Storage:
    env = dict(os.environ) if env is None else env
    supabase = [env.get(name, "").strip() for name in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_BUCKET")]
    if all(supabase):
        return SupabaseStorage(*supabase)
    if any(supabase):
        raise StorageError("set all of SUPABASE_URL, SUPABASE_SERVICE_KEY and SUPABASE_BUCKET, or none")
    if env.get("VERCEL"):
        raise StorageError("on Vercel, certificates need Supabase Storage: set SUPABASE_URL, "
                           "SUPABASE_SERVICE_KEY and SUPABASE_BUCKET")
    default = Path(__file__).resolve().parent / ".data"
    return LocalStorage(Path(env.get("CITED_DATA_DIR") or default))
