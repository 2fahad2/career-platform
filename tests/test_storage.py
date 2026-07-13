"""StorageAdapter tests — no database required.

Covers the atomic-write contract (§15.7 applied at the storage layer), tenant
key prefixing (§10), and traversal safety.
"""

from __future__ import annotations

import pytest

from career.storage.adapter import tenant_key
from career.storage.filesystem import FilesystemStorageAdapter


def test_tenant_key_prefixes_by_tenant() -> None:
    key = tenant_key("abc", "documents", "doc-1.pdf")
    assert key == "tenants/abc/documents/doc-1.pdf"


def test_put_get_roundtrip(tmp_path) -> None:
    store = FilesystemStorageAdapter(tmp_path)
    key = tenant_key("t1", "documents", "cv.pdf")
    store.put(key, b"%PDF-1.7 hello", content_type="application/pdf")
    assert store.exists(key)
    assert store.get(key) == b"%PDF-1.7 hello"


def test_put_overwrite_leaves_no_partial(tmp_path) -> None:
    store = FilesystemStorageAdapter(tmp_path)
    key = tenant_key("t1", "a.bin")
    store.put(key, b"first")
    store.put(key, b"second-longer")
    assert store.get(key) == b"second-longer"
    # No leftover temp files from the atomic write.
    leftovers = list((tmp_path / "tenants" / "t1").glob(".*.tmp-*"))
    assert leftovers == []


def test_get_missing_raises_keyerror(tmp_path) -> None:
    store = FilesystemStorageAdapter(tmp_path)
    with pytest.raises(KeyError):
        store.get("tenants/t1/nope.bin")


def test_delete_is_idempotent(tmp_path) -> None:
    store = FilesystemStorageAdapter(tmp_path)
    key = tenant_key("t1", "x.bin")
    store.put(key, b"x")
    store.delete(key)
    store.delete(key)  # no error the second time
    assert not store.exists(key)


def test_key_traversal_rejected(tmp_path) -> None:
    store = FilesystemStorageAdapter(tmp_path)
    with pytest.raises(ValueError):
        store.put("../escape.bin", b"nope")
