"""交易计划、生命周期与复盘领域模型, 不包含任何 broker 执行概念。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue


class PlanStatus(StrEnum):
    """交易计划生命周期状态的稳定枚举。

    Attributes:
        DRAFT: 草稿态，可继续补充或修订风险关键字段。
        ACTIVE: 已经通过审批并进入观察执行的激活态。
        TRIGGERED: 计划触发，等待或进入后续人工复盘。
        CANCELLED: 计划被人工取消，不再继续观察。
        EXPIRED: 计划因时效失效而结束。
        REVIEWED: 计划已完成复盘闭环。

    Invariants:
        - 枚举值是计划状态机、持久化与 API 返回共同依赖的稳定协议字段。
    """

    DRAFT = "draft"
    ACTIVE = "active"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REVIEWED = "reviewed"


class ReviewOutcome(StrEnum):
    """人工复盘结论的稳定枚举。

    Attributes:
        USEFUL: 复盘认为该计划或扫描结果对决策有帮助。
        FALSE_POSITIVE: 复盘认为存在误报，结论不应触发当前行动。
        FALSE_NEGATIVE: 复盘认为遗漏了本应识别的机会或风险。
        EXECUTED: 复盘记录该计划已被人工实际执行。
        IGNORED: 复盘记录用户选择忽略该计划或结果。
        OTHER: 其他无法归入既有分类的复盘结论。

    Invariants:
        - 枚举值是复盘输出与训练反馈路由依赖的稳定协议字段。
    """

    USEFUL = "useful"
    FALSE_POSITIVE = "false_positive"
    FALSE_NEGATIVE = "false_negative"
    EXECUTED = "executed"
    IGNORED = "ignored"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PlanLineage:
    """计划或复盘所引用的精确、不可变来源版本。

    Attributes:
        source_type: research artifact、scan result 或用户请求。
        source_id: 来源对象稳定标识。
        source_version: 来源精确版本。
        evidence_ids: 支撑来源结论的证据快照标识。
        strategy_id: 可选策略标识。
        strategy_version: 与 strategy_id 成对出现的策略版本。
        model_version_id: 产生扫描结果的已批准量化模型版本。

    Invariants:
        - 来源 ID 非空且版本从 1 开始。
        - Strategy ID 与 version 必须同时提供。
    """

    source_type: str
    source_id: str
    source_version: int
    evidence_ids: tuple[str, ...] = ()
    strategy_id: str | None = None
    strategy_version: int | None = None
    model_version_id: str | None = None

    def __post_init__(self) -> None:
        if self.source_type not in {"research_artifact", "scan_result", "user_request"}:
            raise ValueError("计划来源必须是 research artifact、scan result 或用户请求")
        if not self.source_id.strip() or self.source_version < 1:
            raise ValueError("计划来源必须包含有效 id 与版本")
        if self.strategy_version is not None and self.strategy_version < 1:
            raise ValueError("strategy_version 必须从 1 开始")
        if self.strategy_version is not None and not self.strategy_id:
            raise ValueError("strategy_version 必须与 strategy_id 同时提供")
        if self.strategy_id is not None and self.strategy_version is None:
            raise ValueError("strategy_id 必须与 strategy_version 同时提供")
        if any(not evidence_id.strip() for evidence_id in self.evidence_ids):
            raise ValueError("evidence id 不能为空")


@dataclass(frozen=True, slots=True)
class PlanTransition:
    """交易计划的一次不可变状态迁移记录。

    Attributes:
        from_status: 迁移前状态。
        to_status: 迁移后状态。
        actor_id: 发起迁移的用户或系统角色。
        occurred_at: 带时区的迁移时间。
        reason: 可审计原因。
        approval_interaction_id: 激活等受控迁移对应的 HITL 标识。

    Invariants:
        - actor_id 与 reason 不能为空白字符串。
        - occurred_at 必须包含时区。
    """

    from_status: PlanStatus
    to_status: PlanStatus
    actor_id: str
    occurred_at: datetime
    reason: str
    approval_interaction_id: str | None = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.reason.strip():
            raise ValueError("状态迁移必须记录 actor 与原因")
        if self.occurred_at.tzinfo is None:
            raise ValueError("状态迁移时间必须包含时区")


_RISK_CRITICAL_FIELDS = (
    "horizon",
    "entry_condition",
    "invalidation_condition",
    "target",
    "position_notes",
    "risk_notes",
)


@dataclass(frozen=True, slots=True)
class TradingPlan:
    """仅用于研究决策、绝不代表真实订单的版本化交易计划。

    Attributes:
        plan_id: 计划稳定标识。
        owner_id: 资源所有者。
        security_id: 规范化美股证券标识。
        version: 计划版本，从 1 开始。
        status: 当前领域生命周期状态。
        direction: 计划方向或研究逻辑。
        horizon: 计划有效周期。
        entry_condition: 人工判断的入场条件。
        invalidation_condition: 失效或风险退出条件。
        target: 目标或复核条件。
        position_notes: 仓位与风险预算说明。
        risk_notes: 风险和不确定性说明。
        source_references: 精确 research/scan/user lineage。
        created_at: 当前版本创建时间。
        field_sources: 每个字段的来源说明。
        transitions: 历史状态迁移记录。
        supersedes_version: 当前草稿替代的旧版本。

    Invariants:
        - 只允许规范化美股证券，且至少关联一个来源。
        - 激活前所有风险关键字段必须完整并通过 HITL。
        - 新版本追加创建，不能原地覆盖历史版本。
        - 本模型没有 broker、order、fill 或 account 字段。
    """

    plan_id: str
    owner_id: str
    security_id: str
    version: int
    status: PlanStatus
    direction: str
    horizon: str | None
    entry_condition: str | None
    invalidation_condition: str | None
    target: str | None
    position_notes: str | None
    risk_notes: str | None
    source_references: tuple[PlanLineage, ...]
    created_at: datetime
    field_sources: Mapping[str, str] = field(default_factory=dict)
    transitions: tuple[PlanTransition, ...] = ()
    supersedes_version: int | None = None

    def __post_init__(self) -> None:
        if not self.plan_id.strip() or not self.owner_id.strip():
            raise ValueError("计划必须包含 plan_id 与 owner_id")
        if not _is_us_security(self.security_id):
            raise ValueError("unsupported_market: 首版只支持规范化美股证券")
        if self.version < 1:
            raise ValueError("计划版本必须从 1 开始")
        if not self.direction.strip():
            raise ValueError("计划必须包含方向或逻辑")
        if not self.source_references:
            raise ValueError("计划草稿必须关联 research、scan 或用户请求来源")
        if self.created_at.tzinfo is None:
            raise ValueError("计划创建时间必须包含时区")
        if self.supersedes_version is not None and self.supersedes_version >= self.version:
            raise ValueError("supersedes_version 必须早于当前版本")
        object.__setattr__(self, "field_sources", MappingProxyType(dict(self.field_sources)))

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in _RISK_CRITICAL_FIELDS
            if not _has_text(getattr(self, field_name))
        )

    @property
    def approval_payload_hash(self) -> str:
        payload = {
            "plan_id": self.plan_id,
            "owner_id": self.owner_id,
            "security_id": self.security_id,
            "version": self.version,
            "direction": self.direction,
            "horizon": self.horizon,
            "entry_condition": self.entry_condition,
            "invalidation_condition": self.invalidation_condition,
            "target": self.target,
            "position_notes": self.position_notes,
            "risk_notes": self.risk_notes,
            "source_references": [
                {
                    "source_type": item.source_type,
                    "source_id": item.source_id,
                    "source_version": item.source_version,
                    "evidence_ids": list(item.evidence_ids),
                    "strategy_id": item.strategy_id,
                    "strategy_version": item.strategy_version,
                    "model_version_id": item.model_version_id,
                }
                for item in self.source_references
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode()).hexdigest()

    def revised(
        self,
        *,
        direction: str,
        horizon: str | None,
        entry_condition: str | None,
        invalidation_condition: str | None,
        target: str | None,
        position_notes: str | None,
        risk_notes: str | None,
        source_references: tuple[PlanLineage, ...],
        field_sources: Mapping[str, str],
        created_at: datetime,
    ) -> TradingPlan:
        return TradingPlan(
            plan_id=self.plan_id,
            owner_id=self.owner_id,
            security_id=self.security_id,
            version=self.version + 1,
            status=PlanStatus.DRAFT,
            direction=direction,
            horizon=horizon,
            entry_condition=entry_condition,
            invalidation_condition=invalidation_condition,
            target=target,
            position_notes=position_notes,
            risk_notes=risk_notes,
            source_references=source_references,
            created_at=created_at,
            field_sources=field_sources,
            transitions=self.transitions,
            supersedes_version=self.version,
        )


@dataclass(frozen=True, slots=True)
class Review:
    """对计划或扫描结果的版本化人工复盘。

    Attributes:
        review_id: 复盘稳定标识。
        owner_id: 资源所有者。
        subject_type: plan 或 scan_result。
        subject_id: 被复盘对象标识。
        subject_version: 被复盘对象精确版本。
        outcome: 有用、误报、漏报等复盘结论。
        created_at: 带时区的复盘时间。
        lineage: 复盘引用的不可变来源。
        annotations: 结构化人工备注。
        feedback_destinations: 只允许流向未来策略草稿或未来训练数据。

    Invariants:
        - 复盘不会回写历史计划或扫描结果。
        - feedback_destinations 只能写入允许的未来反馈通道。
    """

    review_id: str
    owner_id: str
    subject_type: str
    subject_id: str
    subject_version: int
    outcome: ReviewOutcome
    created_at: datetime
    lineage: tuple[PlanLineage, ...]
    annotations: Mapping[str, JsonValue] = field(default_factory=dict)
    feedback_destinations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.subject_type not in {"plan", "scan_result"}:
            raise ValueError("复盘对象只能是 plan 或 scan_result")
        if self.subject_version < 1:
            raise ValueError("复盘对象版本必须从 1 开始")
        if self.created_at.tzinfo is None:
            raise ValueError("复盘时间必须包含时区")
        allowed_destinations = {"future_strategy_draft", "future_training_data"}
        if not set(self.feedback_destinations).issubset(allowed_destinations):
            raise ValueError("复盘反馈只能进入未来策略草稿或未来训练数据")
        object.__setattr__(self, "annotations", MappingProxyType(dict(self.annotations)))


_ALLOWED_TRANSITIONS: Mapping[PlanStatus, frozenset[PlanStatus]] = {
    PlanStatus.DRAFT: frozenset({PlanStatus.ACTIVE, PlanStatus.CANCELLED, PlanStatus.EXPIRED}),
    PlanStatus.ACTIVE: frozenset(
        {PlanStatus.TRIGGERED, PlanStatus.CANCELLED, PlanStatus.EXPIRED, PlanStatus.REVIEWED}
    ),
    PlanStatus.TRIGGERED: frozenset(
        {PlanStatus.CANCELLED, PlanStatus.EXPIRED, PlanStatus.REVIEWED}
    ),
    PlanStatus.CANCELLED: frozenset({PlanStatus.REVIEWED}),
    PlanStatus.EXPIRED: frozenset({PlanStatus.REVIEWED}),
    PlanStatus.REVIEWED: frozenset(),
}


def transition_plan(
    plan: TradingPlan,
    *,
    target_status: PlanStatus,
    actor_id: str,
    occurred_at: datetime,
    reason: str,
    approval_interaction_id: str | None = None,
) -> TradingPlan:
    if target_status not in _ALLOWED_TRANSITIONS[plan.status]:
        raise ValueError(f"不允许从 {plan.status.value} 迁移到 {target_status.value}")
    if target_status is PlanStatus.ACTIVE and plan.missing_fields:
        raise ValueError(f"计划仍缺少风险关键字段: {', '.join(plan.missing_fields)}")
    transition = PlanTransition(
        plan.status,
        target_status,
        actor_id,
        occurred_at,
        reason,
        approval_interaction_id,
    )
    return replace(
        plan,
        version=plan.version + 1,
        status=target_status,
        transitions=(*plan.transitions, transition),
        supersedes_version=plan.version,
    )


def _is_us_security(security_id: str) -> bool:
    parts = security_id.split(":")
    return len(parts) >= 3 and parts[0] == "US" and all(part.strip() for part in parts[1:])


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())
