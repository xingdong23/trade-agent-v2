"""只允许小型编排值跨越 graph node 与 checkpoint 边界。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict


class Intent(StrEnum):
    """Supervisor 内置的顶层路由常量。

    说明:
        这些值覆盖当前框架必须保留的稳定语义，其中 ``clarification`` 是全局安全回退。
        业务侧可以通过注册新的 ``AgentManifest.agent_id`` 扩展可路由目标；运行时不会把
        路由空间限制在本枚举内。
    """

    RESEARCH = "research"
    STRATEGY = "strategy"
    PLANNING = "planning"
    CLARIFICATION = "clarification"


type RouteIntent = Intent | str
"""Supervisor 接受的路由输入。

内置 ``Intent`` 负责表达框架级默认语义；字符串用于承载部署时新注册的业务 Agent ID。
"""


DEFAULT_CLARIFICATION_AGENT_ID = Intent.CLARIFICATION.value


def normalize_route_intent(intent: RouteIntent | None) -> str:
    """把内置或扩展意图统一为稳定的路由字符串。

    Args:
        intent: ``Intent`` 枚举、已注册业务 Agent 的字符串 ID，或 ``None``。

    Returns:
        去空白后的稳定路由字符串；无法安全识别时返回 ``clarification``。
    """

    if isinstance(intent, Intent):
        return intent.value
    if isinstance(intent, str):
        normalized = intent.strip()
        if normalized:
            return normalized
    return DEFAULT_CLARIFICATION_AGENT_ID


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Checkpoint 中指向持久化业务产物的轻量引用。

    Attributes:
        artifact_id: Repository 内的稳定产物标识。
        artifact_type: 产物协议类型，用于恢复时选择 repository/presenter。
        version: 被引用产物的不可变版本号。
    """

    artifact_id: str
    artifact_type: str
    version: int


@dataclass(frozen=True, slots=True)
class IntentSchema:
    """经过本地 schema 校验的 Supervisor 路由结果。

    Attributes:
        intent: 允许进入的顶层 Agent 意图或扩展 Agent ID。
        confidence: 分类置信度，范围由分类 adapter 校验。
        reason_code: 稳定审计代码，不包含模型原始推理文本。

    Invariants:
        - 对象不保存 LLM 原始响应，避免 checkpoint 泄漏或膨胀。
    """

    intent: RouteIntent
    confidence: float
    reason_code: str


@dataclass(frozen=True, slots=True)
class ContextReference:
    """Checkpoint 中指向规范化上下文的轻量引用。

    Attributes:
        reference_id: Repository 内的稳定标识。
        reference_type: 上下文类型，用于恢复时选择读取器。
        version: 被引用快照的不可变版本。
    """

    reference_id: str
    reference_type: str
    version: int


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    """适合写入 checkpoint 的稳定错误摘要。

    Attributes:
        code: 跨 provider 稳定的错误码。
        message: 已脱敏、可向用户或日志展示的说明。
        retryable: 调度器是否允许在既定预算内重试。

    Invariants:
        - 不携带 provider 原始 payload、凭据或响应对象。
    """

    code: str
    message: str
    retryable: bool = False


class AgentState(TypedDict, total=False):
    """Supervisor checkpoint schema。

    Attributes:
        user_id: 已认证 owner 标识，是所有后续资源隔离的根。
        thread_id: 会话线程标识。
        run_id: 本次执行标识。
        message: 当前用户输入，大小受 checkpoint 门禁限制。
        intent: 顶层路由意图，既可以是内置常量，也可以是注册型业务 Agent ID。
        intent_result: 经过校验的意图分类摘要。
        context_references: 指向 evidence/context repository 的不可变引用。
        pending_interaction_id: 当前暂停所等待的 HITL 标识。
        artifact: 当前主要输出产物引用。
        error_summary: 可持久化的脱敏错误摘要。
        event_cursor: 已消费事件序号。
        selected_agent_id: Supervisor 选择的 Agent manifest ID。
        policy_decision: Tool 执行前的策略结论。

    Invariants:
        - Evidence、领域 artifact、Tool 参数与 HITL payload 必须保存在各自 repository。
        - State 只保存小型编排值和不可变引用，不能充当业务数据库。
    """

    user_id: str
    thread_id: str
    run_id: str
    message: str
    intent: RouteIntent
    intent_result: IntentSchema
    context_references: tuple[ContextReference, ...]
    pending_interaction_id: str
    artifact: ArtifactReference
    error_summary: ErrorSummary
    event_cursor: int
    selected_agent_id: str
    policy_decision: str


CHECKPOINT_FIELDS = frozenset(AgentState.__annotations__)


def validate_checkpoint_state(state: AgentState) -> None:
    """拒绝把 repository payload 或未声明字段写进 checkpoint。"""
    unknown = set(state) - CHECKPOINT_FIELDS
    if unknown:
        raise ValueError(f"checkpoint 包含未声明字段: {', '.join(sorted(unknown))}")
    message = state.get("message", "")
    if len(message.encode("utf-8")) > 16_384:
        raise ValueError("checkpoint message 超过 16 KiB 限制")
