"""Events emitted by graph and application services."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class RunEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, JsonValue]
    occurred_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AuditEvent:
    audit_id: str
    owner_id: str
    actor_id: str
    action: str
    subject_type: str
    subject_id: str
    occurred_at: datetime
    subject_version: int | None = None
    payload_hash: str | None = None


class EventPublisher(Protocol):
    async def publish(self, event: RunEvent) -> None: ...
