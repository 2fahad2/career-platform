"""Filesystem-backed StorageAdapter.

Writes are atomic (temp → write → fsync → os.replace) so a reader never sees a
half-written object and there are no leftover partials — the atomic-publish
discipline of §15.7, applied at the storage layer. The parent directory is also
fsynced so the rename is durable.
"""

from __future__ import annotations

import os
from pathlib import Path

from career.storage.adapter import StorageAdapter


class FilesystemStorageAdapter(StorageAdapter):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Resolve and confine within root — reject traversal outside the tree.
        p = (self._root / key).resolve()
        if p != self._root and self._root not in p.parents:
            raise ValueError(f"key escapes storage root: {key!r}")
        return p

    def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            # Durably record the rename in the directory entry.
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(key) from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        return sorted(
            str(p.relative_to(self._root))
            for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )
