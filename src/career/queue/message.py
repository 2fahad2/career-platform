"""Queue message envelope.

Every queued task carries the four keys the whitepaper §10 requires:
``tenant_id · run_id · correlation_id · idempotency_key``. The worker never
trusts the payload's authority — it re-verifies tenant ownership from the DB
(§15.11) — but these fields route the work and make redelivery idempotent.

The payload is PII-free by contract (ids and codes only); secrets never appear.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class InvalidMessageError(ValueError):
    """Raised when a queue message cannot be parsed or is missing routing keys."""


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class QueueMessage:
    tenant_id: str
    task_type: str
    run_id: str
    correlation_id: str
    idempotency_key: str
    aggregate_type: str = ""
    aggregate_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=_new_id)

    _REQUIRED = ("tenant_id", "task_type", "run_id", "correlation_id", "idempotency_key")

    def validate(self) -> QueueMessage:
        for name in self._REQUIRED:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InvalidMessageError(f"missing or empty routing key: {name}")
        # tenant_id and aggregate_id (if present) must be valid UUIDs.
        try:
            uuid.UUID(self.tenant_id)
        except (ValueError, AttributeError) as exc:
            raise InvalidMessageError(f"tenant_id is not a valid uuid: {exc}") from exc
        if self.aggregate_id is not None:
            try:
                uuid.UUID(self.aggregate_id)
            except (ValueError, AttributeError) as exc:
                raise InvalidMessageError(
                    f"aggregate_id is not a valid uuid: {exc}"
                ) from exc
        if not isinstance(self.payload, dict):
            raise InvalidMessageError("payload must be an object")
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | bytes) -> QueueMessage:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidMessageError(f"unparseable message: {exc}") from exc
        if not isinstance(data, dict):
            raise InvalidMessageError("message is not an object")
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        unknown = set(data) - known
        if unknown:
            raise InvalidMessageError(f"unknown fields: {sorted(unknown)}")
        try:
            msg = cls(**data)
        except TypeError as exc:
            raise InvalidMessageError(f"bad message shape: {exc}") from exc
        return msg.validate()
