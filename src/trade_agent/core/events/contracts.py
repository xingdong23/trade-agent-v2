"""运行事件与审计事件契约。

可以把这里分成两类：

- ``RunEvent``：描述一次会话流程里发生了什么，主要服务于重放与前端订阅；
- ``AuditEvent``：描述谁对哪个对象做了什么，主要服务于审计与安全追踪。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class RunEvent:
    """一次会话运行中的顺序事件。

    Attributes:
        event_id: 当前事件的唯一标识。
        run_id: 所属会话运行标识。
        sequence: 在同一 run 内的单调递增序号。
        event_type: 事件类型，例如 ``run.started``、``card.created``。
        payload: 事件携带的结构化数据。
        occurred_at: 事件发生时间。
        schema_version: 事件 schema 版本。
    """

    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, JsonValue]
    occurred_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """一条安全审计记录。

    Attributes:
        audit_id: 审计记录唯一标识。
        owner_id: 被操作资源所有者。
        actor_id: 实际执行操作的主体。
        action: 稳定操作代码。
        subject_type: 被操作对象类型。
        subject_id: 被操作对象标识。
        occurred_at: 操作发生时间。
        subject_version: 可选对象精确版本。
        payload_hash: 可选被批准内容摘要。
    """

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
    """异步运行事件发布协议。

    Contract:
        - 实现方必须保留同一 run 内的事件顺序。
        - 发布失败不得伪装成成功，也不能泄漏其他 owner 的事件。

    Implemented by:
        SQLite event store 或外部消息总线 adapter。
    """

    async def publish(self, event: RunEvent) -> None: ...
