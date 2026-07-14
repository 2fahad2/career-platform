"""Queue transport tests — InMemoryQueue (pure) and RedisQueue (live/skip)."""

from __future__ import annotations

import os
import uuid

import pytest

from career.queue.adapter import InMemoryQueue, RedisQueue
from career.queue.message import QueueMessage


def _msg(key: str) -> QueueMessage:
    return QueueMessage(
        tenant_id=str(uuid.uuid4()), task_type="t", run_id="r",
        correlation_id="c", idempotency_key=key,
    )


class TestInMemoryQueue:
    def test_fifo_order(self) -> None:
        q = InMemoryQueue()
        q.enqueue(_msg("a"))
        q.enqueue(_msg("b"))
        assert len(q) == 2
        assert q.dequeue().idempotency_key == "a"  # type: ignore[union-attr]
        assert q.dequeue().idempotency_key == "b"  # type: ignore[union-attr]
        assert q.dequeue() is None


def _redis_client():  # noqa: ANN202
    redis = pytest.importorskip("redis")
    host = os.environ.get("REDIS_HOST", "localhost")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD") or None
    client = redis.Redis(host=host, port=port, password=password,
                         socket_connect_timeout=2)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CI_REQUIRE_DB", "").lower() in {"1", "true", "yes"}:
            pytest.fail(f"Redis not reachable: {exc}")
        pytest.skip(f"Redis not reachable: {exc}")
    return client


class TestRedisQueue:
    def test_roundtrip_fifo(self) -> None:
        client = _redis_client()
        key = f"career:test:{uuid.uuid4()}"
        q = RedisQueue(client, key=key)
        try:
            q.enqueue(_msg("x"))
            q.enqueue(_msg("y"))
            assert q.dequeue(timeout=1).idempotency_key == "x"  # type: ignore[union-attr]
            assert q.dequeue(timeout=1).idempotency_key == "y"  # type: ignore[union-attr]
            assert q.dequeue(timeout=1) is None
        finally:
            client.delete(key)
