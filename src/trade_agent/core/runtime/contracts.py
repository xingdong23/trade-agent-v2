"""只允许小型编排值跨越 graph node 与 checkpoint 边界。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict


class Intent(StrEnum):
    RESEARCH = "research"
    STRATEGY = "strategy"
    PLANNING = "planning"
    CLARIFICATION = "clarification"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    artifact_type: str
    version: int


@dataclass(frozen=True, slots=True)
class IntentSchema:
    """经过本地 schema 校验的路由结果, 不保存 LLM 原始响应。"""

    intent: Intent
    confidence: float
    reason_code: str


@dataclass(frozen=True, slots=True)
class ContextReference:
    """指向 repository 中规范化上下文的轻量引用。"""

    reference_id: str
    reference_type: str
    version: int


@dataclass(frozen=True, slots=True)
class ErrorSummary:
    """适合 checkpoint 的稳定错误摘要, 不携带 provider 原始 payload。"""

    code: str
    message: str
    retryable: bool = False


class AgentState(TypedDict, total=False):
    """Supervisor checkpoint schema。

    Evidence、领域 artifact、tool 参数与 HITL payload 必须保存在各自 repository;
    state 只记录其不可变引用。
    """

    user_id: str
    thread_id: str
    run_id: str
    message: str
    intent: Intent
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
