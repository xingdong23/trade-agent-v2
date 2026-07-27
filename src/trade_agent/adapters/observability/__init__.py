"""结构化本地 trace 与脱敏 export adapter。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.security import Redactor


@dataclass(frozen=True, slots=True)
class TraceEvent:
    correlation_id: str
    event_type: str
    outcome: str
    occurred_at: datetime
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


class TraceExporter(Protocol):
    def export(self, event: TraceEvent) -> None: ...


class StructuredTracer:
    def __init__(self, *, redactor: Redactor | None = None, exporter: TraceExporter | None = None):
        self._redactor = redactor or Redactor()
        self._exporter = exporter
        self._events: list[TraceEvent] = []

    def emit(
        self,
        *,
        correlation_id: str,
        event_type: str,
        outcome: str,
        attributes: Mapping[str, JsonValue],
    ) -> TraceEvent:
        sanitized = self._redactor.redact(attributes)
        if not isinstance(sanitized, Mapping):  # pragma: no cover - input 已是 mapping
            raise TypeError("trace attributes 必须是 mapping")
        event = TraceEvent(
            correlation_id,
            event_type,
            outcome,
            datetime.now(UTC),
            dict(sanitized),
        )
        self._events.append(event)
        if self._exporter is not None:
            self._exporter.export(event)
        return event

    def events(self, correlation_id: str | None = None) -> tuple[TraceEvent, ...]:
        if correlation_id is None:
            return tuple(self._events)
        return tuple(item for item in self._events if item.correlation_id == correlation_id)


__all__ = ["StructuredTracer", "TraceEvent", "TraceExporter"]
