"""可持久化的 HITL 模型与仓储协议。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from trade_agent.core.llm.contracts import JsonValue


class InteractionType(StrEnum):
    """人工参与原因，决定前端交互卡片形态。"""

    CLARIFICATION = "clarification"
    APPROVAL = "approval"
    REVIEW = "review"
    CORRECTION = "correction"
    EXCEPTION_RESOLUTION = "exception_resolution"


class InteractionStatus(StrEnum):
    """HITL 聚合允许的生命周期状态。"""

    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HumanInteraction:
    """一次可暂停、恢复和审计的人工交互聚合。

    Attributes:
        interaction_id: 人工交互稳定标识。
        owner_id: 资源所有者，也是权限隔离键。
        interaction_type: 澄清、批准、复核等交互类型。
        status: 当前生命周期状态。
        payload: 投影 Card 所需的安全展示数据。
        version: 乐观并发版本。
        thread_id: 归属会话线程。
        run_id: 归属执行。
        subject_type: 恢复时使用的业务主题类型。
        subject_id: 被批准或修订的业务对象标识。
        subject_version: 用户看到的业务对象精确版本。
        payload_hash: 用户看到内容的完整性摘要。
        response_schema: 后端验证响应所用 JSON Schema。
        created_at: 创建时间，必须包含时区。
        deadline: 可选过期时间，必须包含时区。
        response: 通过 schema 校验后的用户输入。
        resolved_by: 最终处理者标识。
        resolution: confirm、edit、cancel 等解决动作。
        resolved_at: 解决时间。

    Invariants:
        - 版本从 1 开始且状态只能单向推进。
        - Owner、subject version 与 payload hash 必须同时匹配才能解决。
        - 过期 interaction 永远不能自动批准。
    """

    interaction_id: str
    owner_id: str
    interaction_type: InteractionType
    status: InteractionStatus
    payload: Mapping[str, JsonValue]
    version: int
    thread_id: str
    run_id: str
    subject_type: str
    subject_id: str
    subject_version: int
    payload_hash: str
    response_schema: Mapping[str, JsonValue]
    created_at: datetime
    deadline: datetime | None = None
    response: Mapping[str, JsonValue] | None = None
    resolved_by: str | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.version < 1 or self.subject_version < 1:
            raise ValueError("interaction 与 subject version 必须从 1 开始")
        if self.created_at.tzinfo is None or (
            self.deadline is not None and self.deadline.tzinfo is None
        ):
            raise ValueError("HITL 时间必须包含时区")


class InteractionConflictError(RuntimeError):
    pass


class InteractionExpiredError(RuntimeError):
    pass


class HitlRepository(Protocol):
    """HITL 聚合持久化协议。

    Contract:
        - 所有查询和状态迁移必须按 ``owner_id`` 隔离。
        - ``resolve`` 必须原子校验版本、subject version 和 payload hash。
        - 一个 interaction 最多成功解决一次。

    Implemented by:
        ``SQLiteHitlRepository`` 和测试 repository。
    """

    def create(self, interaction: HumanInteraction) -> HumanInteraction: ...

    def get(self, owner_id: str, interaction_id: str) -> HumanInteraction | None: ...

    def list_pending(self, owner_id: str) -> tuple[HumanInteraction, ...]: ...

    def resolve(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        subject_version: int,
        payload_hash: str,
        actor_id: str,
        response: Mapping[str, JsonValue],
        resolution: str,
    ) -> HumanInteraction: ...

    def transition(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        status: InteractionStatus,
        actor_id: str | None = None,
        resolution: str | None = None,
    ) -> HumanInteraction: ...


class HitlService(Protocol):
    """应用入口使用的 HITL 命令协议。

    Contract:
        - 响应必须先通过 interaction 自带的 JSON Schema。
        - Service 不能绕过 repository 的 owner、版本和过期门禁。

    Implemented by:
        ``DefaultHitlService``。
    """

    def create(self, interaction: HumanInteraction) -> HumanInteraction: ...

    def respond(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        subject_version: int,
        payload_hash: str,
        actor_id: str,
        response: Mapping[str, JsonValue],
        resolution: str,
    ) -> HumanInteraction: ...
