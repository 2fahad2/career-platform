"""Object storage behind an S3-compatible interface (whitepaper §10)."""

from career.storage.adapter import StorageAdapter, tenant_key
from career.storage.filesystem import FilesystemStorageAdapter

__all__ = ["FilesystemStorageAdapter", "StorageAdapter", "tenant_key"]
