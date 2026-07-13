"""StorageAdapter — an S3-compatible interface over object storage.

The rest of the system depends only on this interface, never on a concrete
backend. Today it is filesystem-backed (FilesystemStorageAdapter); it can be
swapped for real S3 later without touching upper layers (CLAUDE.md locked
decision). Keys are always tenant-prefixed: ``tenants/<tenant_id>/...``.
PDF bytes live here, never in the database (§10).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


def tenant_key(tenant_id: str, *parts: str) -> str:
    """Build a tenant-prefixed storage key. All access is per-tenant (§10)."""
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"tenants/{tenant_id}/{tail}" if tail else f"tenants/{tenant_id}"


class StorageAdapter(ABC):
    """Minimal S3-compatible surface. Grows as later phases need it."""

    @abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """Write ``data`` at ``key`` atomically. Returns the key. Overwrite-safe:
        a reader never observes a partial object."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Read the object at ``key``. Raises KeyError if absent."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object at ``key``. No error if it does not exist."""
