"""Storage abstraction — local filesystem for dev, GCS for prod.

Phase C (Apr-2026): adds GCSStorage so production files survive Cloud Run
container recycles and redeploys. Selected via STORAGE_BACKEND=gcs env var
plus GCS_BUCKET_NAME. Local dev keeps STORAGE_BACKEND=local for zero-cost
file IO.

API shape
---------
The storage layer returns "storage paths" — opaque strings that the rest
of the app passes around. For local storage the path is an absolute
filesystem path; for GCS it's a `gs://bucket/object` URI. Callers should
NEVER parse these strings — use the abstraction methods.

To process a file (parse, extract, etc.):

    with get_storage().open_local(storage_path) as local_path:
        parsed = parse_document(local_path, ...)

For local storage `open_local` is zero-copy (yields the existing path).
For GCS it downloads to a NamedTemporaryFile and cleans up on exit.

To save uploaded bytes:

    storage_path = get_storage().save(content_bytes, f"uploads/{upload_id}/{filename}")

The returned `storage_path` should be persisted in the DB (uploads.files
JSONB) and used for all subsequent reads.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Common interface for local + cloud storage."""

    # ---- Primary API (Phase C) ----

    @abstractmethod
    def save(self, content: bytes, remote_path: str) -> str:
        """Persist bytes at `remote_path` and return the canonical
        storage path (absolute local path or gs:// URI)."""
        ...

    @abstractmethod
    @contextlib.contextmanager
    def open_local(self, storage_path: str) -> Iterator[Path]:
        """Yield a local Path the caller can read with stdlib tools.

        For LocalStorage: zero-copy (yields the existing path).
        For GCSStorage: downloads to a tempfile and removes it on exit.

        Always use as a context manager so cloud-backed temp files are
        cleaned up even on exception.
        """
        ...

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        ...

    @abstractmethod
    def delete(self, storage_path: str) -> None:
        """Idempotent — silent no-op if the file is already gone."""
        ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under `prefix` (e.g. an entire upload's
        folder). Returns the count deleted. Idempotent."""
        ...

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return every storage_path under `prefix`."""
        ...

    @abstractmethod
    def public_url(self, storage_path: str, ttl_seconds: int = 3600) -> str:
        """Return a URL the browser can fetch directly.

        For LocalStorage: returns the FastAPI-served path (the existing
        /api/uploads/{id}/files/{filename} endpoint handles serving).
        For GCSStorage: returns a signed URL with the given TTL.
        """
        ...

    # ---- Helpers ----

    @staticmethod
    def file_hash(file_path: str) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    # ---- Legacy API (kept for backward compat with dedup.py) ----

    @abstractmethod
    def upload(self, local_path: str, remote_path: str) -> str:
        """Legacy: copy a local file to storage. Prefer `save(bytes, ...)`."""
        ...

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> str:
        """Legacy: copy from storage to a local path. Prefer `open_local()`."""
        ...

    @abstractmethod
    def get_url(self, remote_path: str) -> str:
        """Legacy alias for `public_url`."""
        ...


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve(self, storage_path: str) -> Path:
        """Local storage paths are absolute paths under base_dir, or
        relative paths that resolve into base_dir."""
        p = Path(storage_path)
        if p.is_absolute():
            return p
        return self.base_dir / p

    def save(self, content: bytes, remote_path: str) -> str:
        dest = self._resolve(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return str(dest)

    @contextlib.contextmanager
    def open_local(self, storage_path: str) -> Iterator[Path]:
        # Zero-copy — yield the existing path. Stdlib tools can open it directly.
        yield self._resolve(storage_path)

    def exists(self, storage_path: str) -> bool:
        return self._resolve(storage_path).exists()

    def delete(self, storage_path: str) -> None:
        try:
            self._resolve(storage_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("LocalStorage.delete failed for %s: %s", storage_path, e)

    def delete_prefix(self, prefix: str) -> int:
        target = self._resolve(prefix)
        if not target.exists():
            return 0
        if target.is_dir():
            count = sum(1 for _ in target.rglob("*") if _.is_file())
            shutil.rmtree(target, ignore_errors=True)
            return count
        target.unlink(missing_ok=True)
        return 1

    def list_prefix(self, prefix: str) -> list[str]:
        target = self._resolve(prefix)
        if not target.exists():
            return []
        if target.is_dir():
            return sorted(str(p) for p in target.rglob("*") if p.is_file())
        return [str(target)]

    def public_url(self, storage_path: str, ttl_seconds: int = 3600) -> str:
        return str(self._resolve(storage_path))

    # --- legacy ---
    def upload(self, local_path: str, remote_path: str) -> str:
        dest = self._resolve(remote_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return str(dest)

    def download(self, remote_path: str, local_path: str) -> str:
        src = self._resolve(remote_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)
        return local_path

    def get_url(self, remote_path: str) -> str:
        return self.public_url(remote_path)

    def build_path(
        self, upload_id: str, carrier: str, account: str, doc_type: str, filename: str
    ) -> str:
        return f"uploads/{upload_id}/{carrier}/{account}/{doc_type}/{filename}"


# ---------------------------------------------------------------------------
# Google Cloud Storage
# ---------------------------------------------------------------------------


_GCS_PREFIX = "gs://"


def _parse_gs(uri: str) -> tuple[str, str]:
    """gs://bucket/key → (bucket, key). Raises on bad input."""
    if not uri.startswith(_GCS_PREFIX):
        raise ValueError(f"Not a gs:// URI: {uri!r}")
    rest = uri[len(_GCS_PREFIX):]
    bucket, sep, key = rest.partition("/")
    if not bucket or not sep:
        raise ValueError(f"Malformed gs:// URI: {uri!r}")
    return bucket, key


class GCSStorage(StorageBackend):
    """GCS-backed storage. Uses Application Default Credentials.

    Local dev: `gcloud auth application-default login` once, then set
    STORAGE_BACKEND=gcs + GCS_BUCKET_NAME=<bucket>.

    Cloud Run: the service account on the runtime gets Storage Object
    Admin on the bucket; ADC picks it up automatically.
    """

    def __init__(self, bucket_name: str):
        from google.cloud import storage as gcs

        if not bucket_name:
            raise ValueError("GCSStorage requires a non-empty bucket_name")
        self._client = gcs.Client()
        self._bucket_name = bucket_name
        self._bucket = self._client.bucket(bucket_name)

    def _uri(self, key: str) -> str:
        return f"{_GCS_PREFIX}{self._bucket_name}/{key.lstrip('/')}"

    def _key(self, storage_path: str) -> str:
        """Accept either a gs:// URI or a bucket-relative key."""
        if storage_path.startswith(_GCS_PREFIX):
            bucket, key = _parse_gs(storage_path)
            if bucket != self._bucket_name:
                # Allow cross-bucket reads if needed, but log it.
                logger.info("GCSStorage: cross-bucket access %s vs %s", bucket, self._bucket_name)
                return key
            return key
        return storage_path.lstrip("/")

    def save(self, content: bytes, remote_path: str) -> str:
        key = self._key(remote_path)
        blob = self._bucket.blob(key)
        blob.upload_from_string(content)
        return self._uri(key)

    @contextlib.contextmanager
    def open_local(self, storage_path: str) -> Iterator[Path]:
        key = self._key(storage_path)
        blob = self._bucket.blob(key)
        # Preserve the original suffix so libraries that sniff by extension
        # (pdfplumber, openpyxl, docling) work the same as on local disk.
        suffix = Path(key).suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tmp_path = Path(tf.name)
        try:
            blob.download_to_filename(str(tmp_path))
            yield tmp_path
        finally:
            tmp_path.unlink(missing_ok=True)

    def exists(self, storage_path: str) -> bool:
        return self._bucket.blob(self._key(storage_path)).exists()

    def delete(self, storage_path: str) -> None:
        try:
            self._bucket.blob(self._key(storage_path)).delete()
        except Exception as e:
            # google-cloud-storage raises NotFound on missing — treat as no-op
            logger.debug("GCSStorage.delete miss for %s: %s", storage_path, e)

    def delete_prefix(self, prefix: str) -> int:
        key_prefix = self._key(prefix).rstrip("/") + "/"
        blobs = list(self._client.list_blobs(self._bucket_name, prefix=key_prefix))
        for b in blobs:
            try:
                b.delete()
            except Exception as e:
                logger.warning("GCSStorage.delete_prefix: %s — %s", b.name, e)
        return len(blobs)

    def list_prefix(self, prefix: str) -> list[str]:
        key_prefix = self._key(prefix).rstrip("/") + "/"
        return [self._uri(b.name) for b in self._client.list_blobs(self._bucket_name, prefix=key_prefix)]

    def public_url(self, storage_path: str, ttl_seconds: int = 3600) -> str:
        from datetime import timedelta

        blob = self._bucket.blob(self._key(storage_path))
        return blob.generate_signed_url(version="v4", expiration=timedelta(seconds=ttl_seconds), method="GET")

    # --- legacy ---
    def upload(self, local_path: str, remote_path: str) -> str:
        key = self._key(remote_path)
        self._bucket.blob(key).upload_from_filename(local_path)
        return self._uri(key)

    def download(self, remote_path: str, local_path: str) -> str:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._bucket.blob(self._key(remote_path)).download_to_filename(local_path)
        return local_path

    def get_url(self, remote_path: str) -> str:
        return self.public_url(remote_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_STORAGE: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Singleton storage instance, picked from settings."""
    global _STORAGE
    if _STORAGE is not None:
        return _STORAGE
    from backend.settings import settings

    backend = (settings.storage_backend or "local").lower()
    if backend == "local":
        _STORAGE = LocalStorage(settings.storage_base_dir)
    elif backend == "gcs":
        _STORAGE = GCSStorage(settings.gcs_bucket_name)
    else:
        raise ValueError(f"Unknown storage backend: {settings.storage_backend!r}")
    logger.info("Initialized %s storage backend", backend)
    return _STORAGE


def reset_storage() -> None:
    """Drop the singleton — used in tests + to switch backends mid-process."""
    global _STORAGE
    _STORAGE = None
