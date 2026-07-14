"""Queue transport — a small interface with an in-memory and a Redis backend.

The worker and tests depend only on the ``Queue`` protocol. InMemoryQueue makes
the worker loop deterministically testable without a broker; RedisQueue is the
runtime transport (a Redis list used as a FIFO).
"""

from __future__ import annotations

from collections import deque
from typing import Protocol

from career.queue.message import QueueMessage


class Queue(Protocol):
    def enqueue(self, message: QueueMessage) -> None: ...
    def dequeue(self, *, timeout: float = 0.0) -> QueueMessage | None: ...


class InMemoryQueue:
    """Deterministic FIFO for tests. Not thread-safe by design (single-threaded
    test use)."""

    def __init__(self) -> None:
        self._items: deque[str] = deque()

    def enqueue(self, message: QueueMessage) -> None:
        self._items.append(message.validate().to_json())

    def dequeue(self, *, timeout: float = 0.0) -> QueueMessage | None:
        if not self._items:
            return None
        return QueueMessage.from_json(self._items.popleft())

    def __len__(self) -> int:
        return len(self._items)


class RedisQueue:
    """Redis list as a FIFO queue (LPUSH producer / BRPOP consumer)."""

    def __init__(self, client: object, key: str = "career:queue:default") -> None:
        self._client = client
        self._key = key

    def enqueue(self, message: QueueMessage) -> None:
        self._client.lpush(self._key, message.validate().to_json())  # type: ignore[attr-defined]

    def dequeue(self, *, timeout: float = 1.0) -> QueueMessage | None:
        result = self._client.brpop([self._key], timeout=timeout)  # type: ignore[attr-defined]
        if result is None:
            return None
        _key, raw = result
        return QueueMessage.from_json(raw)
