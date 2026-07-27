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
    """一次结构化 trace 事件的标准记录。

    Attributes:
        correlation_id: 贯穿一次请求或运行链路的追踪 ID。
        event_type: 事件种类，例如 tool.invoke 或 hitl.pause。
        outcome: 结果标签，例如 success 或 error。
        occurred_at: 事件发生时间，UTC 时间戳。
        attributes: 经过脱敏后的附加属性。
    """

    correlation_id: str
    event_type: str
    outcome: str
    occurred_at: datetime
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


class TraceExporter(Protocol):
    """向外部系统导出 trace 事件的协议。

    Contract:
        - 实现方不得修改传入的 TraceEvent。
        - 导出失败策略必须由实现方显式决定，不能静默破坏本地 trace 记录。

    Implemented by:
        未来的 OTLP / 文件 exporter adapter
        测试中的 exporter fake
    """

    def export(self, event: TraceEvent) -> None:
        """导出一个已经脱敏的 trace 事件。

        Args:
            event: 需要发送到外部后端的稳定 trace 记录。
        """
        ...


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
