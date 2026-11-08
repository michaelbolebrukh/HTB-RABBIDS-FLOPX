import os
import shutil
from typing import Optional

from platform.services.storage.base import ObjectStorage


class LocalObjectStorage(ObjectStorage):
    """Writes files under a local directory served by the /uploads static mount.

    Suitable for development and single-host deployments with a persistent disk.
    The private plane lives in a sibling directory (`<base_dir>-private` by
    default) that is NEVER mounted, so private objects have no ambient URL —
    reads go through read_private only.

    Deliberately NOT durable. On a laptop this directory persists happily, but
    the platform cannot prove that from inside the process: on a container host
    the same code writes to a filesystem that is discarded on every deploy, and
    claiming durability there is what silently loses files. Treating local disk
    as best-effort everywhere costs nothing in development (the durability gate
    only bites in production) and removes the failure mode entirely.
    """

    is_durable = False

    def __init__(self, base_dir: str, public_origin: Optional[str], private_dir: Optional[str] = None) -> None:
        self.base_dir = base_dir
        self.private_dir = private_dir or f"{base_dir.rstrip('/')}-private"
        self.public_origin = (public_origin or "").rstrip("/")
        os.makedirs(self.base_dir, exist_ok=True)

    @staticmethod
    def _resolve_inside(base_dir: str, key: str) -> str:
        # Guarding against path traversal — the resolved path must stay inside base_dir:
        base = os.path.abspath(base_dir)
        path = os.path.abspath(os.path.join(base, key))
        if path != base and not path.startswith(base + os.sep):
            raise ValueError("Invalid storage key")
        return path

    def _write(self, base_dir: str, data: bytes, key: str) -> str:
        path = self._resolve_inside(base_dir, key)
        if path == os.path.abspath(base_dir):
            raise ValueError("Invalid storage key")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    @staticmethod
    def _delete_under(base_dir: str, prefix: str) -> None:
        # Same traversal guard as save — the resolved path must stay inside base_dir:
        base = os.path.abspath(base_dir)
        path = os.path.abspath(os.path.join(base, prefix.rstrip("/")))
        if path == base or not path.startswith(base + os.sep):
            raise ValueError("Invalid storage prefix")
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)

    def save(self, data: bytes, key: str, content_type: str) -> str:
        self._write(self.base_dir, data, key)
        # Served by the StaticFiles mount at /uploads (see main.py):
        return f"{self.public_origin}/uploads/{key}"

    def delete_prefix(self, prefix: str) -> None:
        self._delete_under(self.base_dir, prefix)

    def save_private(self, data: bytes, key: str, content_type: str) -> None:
        self._write(self.private_dir, data, key)

    def read_private(self, key: str) -> bytes:
        path = self._resolve_inside(self.private_dir, key)
        with open(path, "rb") as handle:
            return handle.read()

    def delete_private_prefix(self, prefix: str) -> None:
        self._delete_under(self.private_dir, prefix)
