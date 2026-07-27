"""Planning capability 的应用层用例编排。

这层不直接决定“什么是好计划”，那是 domain 的工作；它负责把一次命令变成确定性
状态迁移，并处理幂等、版本保护、审批凭证和跨对象引用。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock

from trade_agent.capabilities.planning.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
    PlanLineage,
    PlanStatus,
    Review,
    ReviewOutcome,
    TradingPlan,
)
from trade_agent.capabilities.planning.domain import transition_plan
from trade_agent.core.llm.contracts import JsonValue


class PlanningConflictError(RuntimeError):
    """幂等键或聚合版本与当前状态冲突。"""


@dataclass(frozen=True, slots=True)
class PlanDraftRequest:
    """创建或修订计划草稿时的标准输入。

    这个对象把来自表单、Research journey 或 Tool 调用的输入统一成一种形状，
    避免 application service 直接依赖某个入口层的参数命名。

    Attributes:
        plan_id: 计划稳定标识。
        owner_id: 计划所属用户。
        security_id: 已解析的规范证券标识。
        direction: 方向或交易逻辑摘要。
        created_at: 本次草稿创建或修订时间。
        source_references: 研究、扫描或用户输入的来源关系集合。
        horizon: 计划周期；未补齐时可为空。
        entry_condition: 入场条件；未补齐时可为空。
        invalidation_condition: 失效或止损条件；未补齐时可为空。
        target: 目标条件；未补齐时可为空。
        position_notes: 仓位备注；未补齐时可为空。
        risk_notes: 风险说明；未补齐时可为空。
        field_sources: 每个字段当前值的来源说明。
    """

    plan_id: str
    owner_id: str
    security_id: str
    direction: str
    created_at: datetime
    source_references: tuple[PlanLineage, ...]
    horizon: str | None = None
    entry_condition: str | None = None
    invalidation_condition: str | None = None
    target: str | None = None
    position_notes: str | None = None
    risk_notes: str | None = None
    field_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """封装一次复盘命令的返回结果。

    Attributes:
        review: 已持久化的复盘记录。
        reviewed_plan: 若复盘对象是计划，则返回被推进到 reviewed 的计划版本；否则为空。
    """

    review: Review
    reviewed_plan: TradingPlan | None = None


class PlanningApplication:
    """保留统一 capability command/query 边界, 由具体入口使用类型化 service。"""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"planning command 尚未接入通用 dispatcher: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"planning query 尚未接入通用 dispatcher: {query.query_id}")


class PlanningService:
    """Planning capability 的进程内确定性实现。

    教学上可以把它看成“无数据库版的 application service”：

    - ``_plans`` / ``_reviews`` 存状态；
    - ``_draft_commands`` / ``_transition_commands`` / ``_review_commands`` 存幂等收据；
    - ``Lock`` 让单进程示例也具备最小并发保护语义。
    """

    def __init__(self) -> None:
        self._plans: dict[tuple[str, str], list[TradingPlan]] = {}
        self._reviews: dict[tuple[str, str], Review] = {}
        self._draft_commands: dict[tuple[str, str], tuple[str, TradingPlan]] = {}
        self._transition_commands: dict[tuple[str, str], tuple[str, TradingPlan]] = {}
        self._review_commands: dict[tuple[str, str], tuple[str, ReviewResult]] = {}
        self._lock = Lock()

    def create_draft(self, request: PlanDraftRequest, *, idempotency_key: str) -> TradingPlan:
        """创建第一个草稿版本。"""

        _require_idempotency_key(idempotency_key)
        fingerprint = _draft_fingerprint(request, expected_version=None)
        command_key = (request.owner_id, idempotency_key)
        with self._lock:
            replay = self._draft_commands.get(command_key)
            if replay is not None:
                return _replay_or_conflict(replay, fingerprint, "计划草稿")
            plan_key = (request.owner_id, request.plan_id)
            if plan_key in self._plans:
                raise PlanningConflictError("计划已存在, 编辑必须创建新的草稿版本")
            # version=1 的草稿是后续审批、修订和 artifact 投影的唯一起点。
            plan = TradingPlan(
                plan_id=request.plan_id,
                owner_id=request.owner_id,
                security_id=request.security_id,
                version=1,
                status=PlanStatus.DRAFT,
                direction=request.direction,
                horizon=request.horizon,
                entry_condition=request.entry_condition,
                invalidation_condition=request.invalidation_condition,
                target=request.target,
                position_notes=request.position_notes,
                risk_notes=request.risk_notes,
                source_references=request.source_references,
                created_at=request.created_at,
                field_sources=request.field_sources,
            )
            self._plans[plan_key] = [plan]
            self._draft_commands[command_key] = (fingerprint, plan)
            return plan

    def revise_draft(
        self,
        request: PlanDraftRequest,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> TradingPlan:
        """基于当前 draft 生成一个新的草稿版本。"""

        _require_idempotency_key(idempotency_key)
        fingerprint = _draft_fingerprint(request, expected_version=expected_version)
        command_key = (request.owner_id, idempotency_key)
        with self._lock:
            replay = self._draft_commands.get(command_key)
            if replay is not None:
                return _replay_or_conflict(replay, fingerprint, "计划草稿修订")
            current = self._current_unlocked(request.owner_id, request.plan_id)
            if current.version != expected_version:
                raise PlanningConflictError("计划版本已变化, 请基于最新草稿重新编辑")
            if current.status is not PlanStatus.DRAFT:
                raise ValueError("只有 draft 计划可以直接编辑")
            if request.security_id != current.security_id:
                raise ValueError("编辑计划不得替换已解析证券")
            # 修订不会原地修改旧版本，而是追加新版本，便于审批和审计追踪。
            revised = current.revised(
                direction=request.direction,
                horizon=request.horizon,
                entry_condition=request.entry_condition,
                invalidation_condition=request.invalidation_condition,
                target=request.target,
                position_notes=request.position_notes,
                risk_notes=request.risk_notes,
                source_references=request.source_references,
                field_sources=request.field_sources,
                created_at=request.created_at,
            )
            self._plans[(request.owner_id, request.plan_id)].append(revised)
            self._draft_commands[command_key] = (fingerprint, revised)
            return revised

    def activate(
        self,
        *,
        owner_id: str,
        plan_id: str,
        expected_version: int,
        actor_id: str,
        approved: bool,
        approved_payload_hash: str,
        approval_interaction_id: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> TradingPlan:
        """在明确批准后把 draft 激活为 active。"""

        if not approved:
            raise PermissionError("激活计划必须经过 owner 明确批准")
        if actor_id != owner_id:
            raise PermissionError("只有计划 owner 可以批准激活")
        if not approval_interaction_id.strip():
            raise ValueError("激活计划必须关联 HITL approval interaction")
        payload: dict[str, JsonValue] = {
            "owner_id": owner_id,
            "plan_id": plan_id,
            "expected_version": expected_version,
            "actor_id": actor_id,
            "approved_payload_hash": approved_payload_hash,
            "approval_interaction_id": approval_interaction_id,
            "occurred_at": occurred_at.isoformat(),
        }
        fingerprint = _fingerprint(payload)
        command_key = (owner_id, idempotency_key)
        _require_idempotency_key(idempotency_key)
        with self._lock:
            replay = self._transition_commands.get(command_key)
            if replay is not None:
                return _replay_or_conflict(replay, fingerprint, "计划激活")
            current = self._current_unlocked(owner_id, plan_id)
            if current.version != expected_version:
                raise PlanningConflictError("计划版本已变化, 不能批准旧草稿")
            if approved_payload_hash != current.approval_payload_hash:
                raise PlanningConflictError("审批 payload hash 与当前草稿不一致")
            # approval_payload_hash 把“用户批准的内容”与当前草稿绑定在一起，
            # 防止前端提交过期审批卡片却仍激活最新版本。
            activated = transition_plan(
                current,
                target_status=PlanStatus.ACTIVE,
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason="用户批准激活交易计划",
                approval_interaction_id=approval_interaction_id,
            )
            self._plans[(owner_id, plan_id)].append(activated)
            self._transition_commands[command_key] = (fingerprint, activated)
            return activated

    def transition(
        self,
        *,
        owner_id: str,
        plan_id: str,
        expected_version: int,
        target_status: PlanStatus,
        actor_id: str,
        reason: str,
        approval_interaction_id: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> TradingPlan:
        """执行除 active 之外的受控状态迁移。"""

        if target_status is PlanStatus.ACTIVE:
            raise ValueError("active 迁移必须使用带审批 payload hash 的 activate")
        if actor_id != owner_id:
            raise PermissionError("只有计划 owner 可以执行受控迁移")
        if not approval_interaction_id.strip():
            raise ValueError("受控计划迁移必须关联 HITL interaction")
        _require_idempotency_key(idempotency_key)
        payload: dict[str, JsonValue] = {
            "owner_id": owner_id,
            "plan_id": plan_id,
            "expected_version": expected_version,
            "target_status": target_status.value,
            "actor_id": actor_id,
            "reason": reason,
            "approval_interaction_id": approval_interaction_id,
            "occurred_at": occurred_at.isoformat(),
        }
        fingerprint = _fingerprint(payload)
        command_key = (owner_id, idempotency_key)
        with self._lock:
            replay = self._transition_commands.get(command_key)
            if replay is not None:
                return _replay_or_conflict(replay, fingerprint, "计划状态迁移")
            current = self._current_unlocked(owner_id, plan_id)
            if current.version != expected_version:
                raise PlanningConflictError("计划版本已变化, 不能执行旧状态迁移")
            transitioned = transition_plan(
                current,
                target_status=target_status,
                actor_id=actor_id,
                occurred_at=occurred_at,
                reason=reason,
                approval_interaction_id=approval_interaction_id,
            )
            self._plans[(owner_id, plan_id)].append(transitioned)
            self._transition_commands[command_key] = (fingerprint, transitioned)
            return transitioned

    def record_review(
        self,
        *,
        owner_id: str,
        review_id: str,
        subject_type: str,
        subject_id: str,
        subject_version: int,
        outcome: ReviewOutcome,
        annotations: Mapping[str, JsonValue],
        lineage: tuple[PlanLineage, ...],
        feedback_destinations: tuple[str, ...],
        actor_id: str,
        approval_interaction_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> ReviewResult:
        """记录计划或扫描结果的人工复盘。"""

        if actor_id != owner_id:
            raise PermissionError("只有 owner 可以提交复盘")
        if not approval_interaction_id.strip():
            raise ValueError("复盘提交必须关联 HITL review interaction")
        _require_idempotency_key(idempotency_key)
        payload: dict[str, JsonValue] = {
            "owner_id": owner_id,
            "review_id": review_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "subject_version": subject_version,
            "outcome": outcome.value,
            "annotations": dict(annotations),
            "feedback_destinations": list(feedback_destinations),
            "actor_id": actor_id,
            "approval_interaction_id": approval_interaction_id,
            "created_at": created_at.isoformat(),
            "lineage": [_lineage_payload(item) for item in lineage],
        }
        fingerprint = _fingerprint(payload)
        command_key = (owner_id, idempotency_key)
        with self._lock:
            replay = self._review_commands.get(command_key)
            if replay is not None:
                return _replay_or_conflict(replay, fingerprint, "计划复盘")
            if (owner_id, review_id) in self._reviews:
                raise PlanningConflictError("review_id 已存在")

            reviewed_plan: TradingPlan | None = None
            resolved_lineage = lineage
            if subject_type == "plan":
                # 计划复盘会顺带把对应计划推进到 reviewed，形成后续学习样本。
                current = self._current_unlocked(owner_id, subject_id)
                if current.version != subject_version:
                    raise PlanningConflictError("复盘必须引用当前计划的精确版本")
                resolved_lineage = current.source_references
                reviewed_plan = transition_plan(
                    current,
                    target_status=PlanStatus.REVIEWED,
                    actor_id=actor_id,
                    occurred_at=created_at,
                    reason="用户提交计划复盘",
                    approval_interaction_id=approval_interaction_id,
                )
            elif subject_type == "scan_result":
                if not lineage or any(item.source_type != "scan_result" for item in lineage):
                    raise ValueError("scan result 复盘必须保留 scan lineage")
            else:
                raise ValueError("复盘对象只能是 plan 或 scan_result")

            review = Review(
                review_id=review_id,
                owner_id=owner_id,
                subject_type=subject_type,
                subject_id=subject_id,
                subject_version=subject_version,
                outcome=outcome,
                created_at=created_at,
                lineage=resolved_lineage,
                annotations=annotations,
                feedback_destinations=feedback_destinations,
            )
            self._reviews[(owner_id, review_id)] = review
            if reviewed_plan is not None:
                self._plans[(owner_id, subject_id)].append(reviewed_plan)
            result = ReviewResult(review, reviewed_plan)
            self._review_commands[command_key] = (fingerprint, result)
            return result

    def get_plan(self, *, owner_id: str, plan_id: str) -> TradingPlan:
        with self._lock:
            return self._current_unlocked(owner_id, plan_id)

    def get_plan_version(self, *, owner_id: str, plan_id: str, version: int) -> TradingPlan:
        if version < 1:
            raise LookupError("计划版本不存在或不属于当前 owner")
        with self._lock:
            try:
                return self._plans[(owner_id, plan_id)][version - 1]
            except (KeyError, IndexError) as exc:
                raise LookupError("计划版本不存在或不属于当前 owner") from exc

    def create_draft_from_mapping(
        self, arguments: Mapping[str, JsonValue], *, idempotency_key: str
    ) -> Mapping[str, JsonValue]:
        request = _draft_request(arguments)
        expected_version = _optional_integer(arguments.get("expected_version"))
        if expected_version is None:
            plan = self.create_draft(request, idempotency_key=idempotency_key)
        else:
            plan = self.revise_draft(
                request,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        return plan_payload(plan)

    def transition_from_mapping(
        self,
        arguments: Mapping[str, JsonValue],
        *,
        approval_interaction_id: str,
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        target_status = PlanStatus(_required_string(arguments, "target_status"))
        owner_id = _required_string(arguments, "owner_id")
        plan_id = _required_string(arguments, "plan_id")
        expected_version = _required_integer(arguments, "expected_version")
        actor_id = _required_string(arguments, "actor_id")
        occurred_at = _required_datetime(arguments, "occurred_at")
        if target_status is PlanStatus.ACTIVE:
            plan = self.activate(
                owner_id=owner_id,
                plan_id=plan_id,
                expected_version=expected_version,
                actor_id=actor_id,
                approved=_required_boolean(arguments, "approved"),
                approved_payload_hash=_required_string(arguments, "approved_payload_hash"),
                approval_interaction_id=approval_interaction_id,
                idempotency_key=idempotency_key,
                occurred_at=occurred_at,
            )
        else:
            plan = self.transition(
                owner_id=owner_id,
                plan_id=plan_id,
                expected_version=expected_version,
                target_status=target_status,
                actor_id=actor_id,
                reason=_required_string(arguments, "reason"),
                approval_interaction_id=approval_interaction_id,
                idempotency_key=idempotency_key,
                occurred_at=occurred_at,
            )
        return plan_payload(plan)

    def record_review_from_mapping(
        self,
        arguments: Mapping[str, JsonValue],
        *,
        approval_interaction_id: str,
        idempotency_key: str,
    ) -> Mapping[str, JsonValue]:
        result = self.record_review(
            owner_id=_required_string(arguments, "owner_id"),
            review_id=_required_string(arguments, "review_id"),
            subject_type=_required_string(arguments, "subject_type"),
            subject_id=_required_string(arguments, "subject_id"),
            subject_version=_required_integer(arguments, "subject_version"),
            outcome=ReviewOutcome(_required_string(arguments, "outcome")),
            annotations=_json_mapping(arguments.get("annotations")),
            lineage=_lineages(arguments.get("lineage")),
            feedback_destinations=_string_tuple(arguments.get("feedback_destinations")),
            actor_id=_required_string(arguments, "actor_id"),
            approval_interaction_id=approval_interaction_id,
            idempotency_key=idempotency_key,
            created_at=_required_datetime(arguments, "created_at"),
        )
        return review_payload(result)

    def _current_unlocked(self, owner_id: str, plan_id: str) -> TradingPlan:
        try:
            return self._plans[(owner_id, plan_id)][-1]
        except (KeyError, IndexError) as exc:
            raise LookupError("计划不存在或不属于当前 owner") from exc


def plan_payload(plan: TradingPlan) -> Mapping[str, JsonValue]:
    """把领域计划转换为稳定的跨层 JSON 载荷。"""

    return {
        "plan_id": plan.plan_id,
        "owner_id": plan.owner_id,
        "security_id": plan.security_id,
        "version": plan.version,
        "status": plan.status.value,
        "direction": plan.direction,
        "horizon": plan.horizon,
        "entry_condition": plan.entry_condition,
        "invalidation_condition": plan.invalidation_condition,
        "target": plan.target,
        "position_notes": plan.position_notes,
        "risk_notes": plan.risk_notes,
        "missing_fields": list(plan.missing_fields),
        "approval_payload_hash": plan.approval_payload_hash,
        "source_references": [_lineage_payload(item) for item in plan.source_references],
        "supersedes_version": plan.supersedes_version,
    }


def review_payload(result: ReviewResult) -> Mapping[str, JsonValue]:
    """把复盘结果转换为 API/Tool 可返回的 JSON 结构。"""

    review = result.review
    return {
        "review_id": review.review_id,
        "subject_type": review.subject_type,
        "subject_id": review.subject_id,
        "subject_version": review.subject_version,
        "outcome": review.outcome.value,
        "lineage": [_lineage_payload(item) for item in review.lineage],
        "feedback_destinations": list(review.feedback_destinations),
        "reviewed_plan_version": (
            result.reviewed_plan.version if result.reviewed_plan is not None else None
        ),
    }


def _draft_request(arguments: Mapping[str, JsonValue]) -> PlanDraftRequest:
    return PlanDraftRequest(
        plan_id=_required_string(arguments, "plan_id"),
        owner_id=_required_string(arguments, "owner_id"),
        security_id=_required_string(arguments, "security_id"),
        direction=_required_string(arguments, "direction"),
        created_at=_required_datetime(arguments, "created_at"),
        source_references=_lineages(arguments.get("source_references")),
        horizon=_optional_string(arguments.get("horizon")),
        entry_condition=_optional_string(arguments.get("entry_condition")),
        invalidation_condition=_optional_string(arguments.get("invalidation_condition")),
        target=_optional_string(arguments.get("target")),
        position_notes=_optional_string(arguments.get("position_notes")),
        risk_notes=_optional_string(arguments.get("risk_notes")),
        field_sources=_string_mapping(arguments.get("field_sources")),
    )


def _lineages(value: JsonValue) -> tuple[PlanLineage, ...]:
    if not isinstance(value, list):
        raise ValueError("lineage 必须是数组")
    result: list[PlanLineage] = []
    for raw_item in value:
        if not isinstance(raw_item, Mapping):
            raise ValueError("lineage item 必须是 object")
        result.append(
            PlanLineage(
                source_type=_required_string(raw_item, "source_type"),
                source_id=_required_string(raw_item, "source_id"),
                source_version=_required_integer(raw_item, "source_version"),
                evidence_ids=_string_tuple(raw_item.get("evidence_ids")),
                strategy_id=_optional_string(raw_item.get("strategy_id")),
                strategy_version=_optional_integer(raw_item.get("strategy_version")),
                model_version_id=_optional_string(raw_item.get("model_version_id")),
            )
        )
    return tuple(result)


def _lineage_payload(lineage: PlanLineage) -> dict[str, JsonValue]:
    return {
        "source_type": lineage.source_type,
        "source_id": lineage.source_id,
        "source_version": lineage.source_version,
        "evidence_ids": list(lineage.evidence_ids),
        "strategy_id": lineage.strategy_id,
        "strategy_version": lineage.strategy_version,
        "model_version_id": lineage.model_version_id,
    }


def _draft_fingerprint(request: PlanDraftRequest, *, expected_version: int | None) -> str:
    payload: dict[str, JsonValue] = {
        "plan_id": request.plan_id,
        "owner_id": request.owner_id,
        "security_id": request.security_id,
        "direction": request.direction,
        "created_at": request.created_at.isoformat(),
        "source_references": [_lineage_payload(item) for item in request.source_references],
        "horizon": request.horizon,
        "entry_condition": request.entry_condition,
        "invalidation_condition": request.invalidation_condition,
        "target": request.target,
        "position_notes": request.position_notes,
        "risk_notes": request.risk_notes,
        "field_sources": dict(request.field_sources),
        "expected_version": expected_version,
    }
    return _fingerprint(payload)


def _fingerprint(payload: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _replay_or_conflict[T](record: tuple[str, T], fingerprint: str, label: str) -> T:
    if record[0] != fingerprint:
        raise PlanningConflictError(f"幂等键对应的{label} payload 已改变")
    return record[1]


def _require_idempotency_key(value: str) -> None:
    if not value.strip():
        raise ValueError("受控 planning 写操作必须提供 idempotency key")


def _required_string(payload: Mapping[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _optional_string(value: JsonValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("可选文本字段必须是字符串或 null")
    stripped = value.strip()
    return stripped or None


def _required_integer(payload: Mapping[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return value


def _optional_integer(value: JsonValue) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("可选版本字段必须是整数或 null")
    return value


def _required_boolean(payload: Mapping[str, JsonValue], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是 boolean")
    return value


def _required_datetime(payload: Mapping[str, JsonValue], key: str) -> datetime:
    value = _required_string(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{key} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{key} 必须包含时区")
    return parsed


def _string_tuple(value: JsonValue) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("期望字符串数组")
    return tuple(item for item in value if isinstance(item, str))


def _string_mapping(value: JsonValue) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("field_sources 必须是 string 到 string 的 object")
    return {str(key): str(item) for key, item in value.items()}


def _json_mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("annotations 必须是 object")
    return dict(value)


__all__ = [
    "PlanDraftRequest",
    "PlanningApplication",
    "PlanningConflictError",
    "PlanningService",
    "ReviewResult",
    "plan_payload",
    "review_payload",
]
