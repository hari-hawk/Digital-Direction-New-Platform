"""Storage abstraction — local filesystem for dev, GCS for prod."""

import hashlib
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def upload(self, local_path: str, remote_path: str) -> str:
        ...

    @abstractmethod
    def download(self, remote_path: str, local_path: str) -> str:
        ...

    @abstractmethod
    def exists(self, remote_path: str) -> bool:
        ...

    @abstractmethod
    def get_url(self, remote_path: str) -> str:
        ...

    @staticmethod
    def file_hash(file_path: str) -> str:
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def upload(self, local_path: str, remote_path: str) -> str:
        dest = self.base_dir / remote_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return str(dest)

    def download(self, remote_path: str, local_path: str) -> str:
        src = self.base_dir / remote_path
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, local_path)
        return local_path

    def exists(self, remote_path: str) -> bool:
        return (self.base_dir / remote_path).exists()

    def get_url(self, remote_path: str) -> str:
        return str(self.base_dir / remote_path)

    def build_path(
        self, upload_id: str, carrier: str, account: str, doc_type: str, filename: str
    ) -> str:
        return f"uploads/{upload_id}/{carrier}/{account}/{doc_type}/{filename}"


def get_storage() -> StorageBackend:
    from backend.settings import settings

    if settings.storage_backend == "local":
        return LocalStorage(settings.storage_base_dir)
    raise ValueError(f"Unknown storage backend: {settings.storage_backend}")
