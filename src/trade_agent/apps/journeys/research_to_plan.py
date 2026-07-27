"""Research -> plan 会话旅程插件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from trade_agent.apps.journeys.contracts import (
    ConversationJourney,
    ConversationRunResult,
    ConversationRuntimePort,
    JourneyStartContext,
)
from trade_agent.capabilities.planning.application import (
    PlanDraftRequest,
    PlanningService,
    review_payload,
)
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage, ReviewOutcome, TradingPlan
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope

JOURNEY_RESEARCH_TO_PLAN = "research_to_plan"

_SUBJECT_SECURITY_CHOICE = "research_security_choice"
_SUBJECT_SCAN_REVIEW = "research_scan_review"
_SUBJECT_PLAN_FORM = "research_plan_form"
_SUBJECT_PLAN_APPROVAL = "research_plan_approval"
_SUBJECT_REMINDER_APPROVAL = "research_reminder_approval"
_SUBJECT_PLAN_REVIEW = "research_plan_review"


@dataclass(frozen=True, slots=True)
class SecurityCandidate:
    """证券代码无法唯一解析时展示给用户的候选项。

    Attributes:
        security_id: 已规范化的稳定证券标识。
        label: 可供用户辨认的展示名称。
    """

    security_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ResearchJourneyResult:
    """Research capability 完成一次准备后交回编排层的结果集合。

    Attributes:
        security_id: 本次研究绑定的规范证券标识。
        research_card: 已完成研究产物 Card。
        scan_progress_started: 扫描开始时的进度 Card。
        scan_progress_completed: 扫描结束时的进度 Card。
        scan_result_card: 已持久化扫描结果的只读 Card。
        plan_values: 后续生成计划草稿所需的结构化事实。

    Invariants:
        - Card 必须来自 capability presenter，journey 不能重算预测或排名。
    """

    security_id: str
    research_card: CardEnvelope
    scan_progress_started: CardEnvelope
    scan_progress_completed: CardEnvelope
    scan_result_card: CardEnvelope
    plan_values: Mapping[str, JsonValue]


class ResearchJourneyBackend(Protocol):
    """研究旅程依赖的应用边界。

    Contract:
        - 实现方负责解析证券、准备研究、总结扫描和激活提醒。
        - ``summarize`` 只能读取已持久化扫描结果，不能参与预测、评分或排序。
        - 所有读取和写入必须使用 ``owner_id`` 隔离，并遵守 idempotency key。

    Implemented by:
        生产 capability façade 和端到端验收测试的 fake backend。
    """

    def resolve(self, symbol: str, *, owner_id: str, run_id: str) -> tuple[SecurityCandidate, ...]:
        """把一个证券代码解析为一个或多个美国上市候选项。"""

    def prepare(self, security_id: str, *, owner_id: str, run_id: str) -> ResearchJourneyResult:
        """准备研究与量化扫描结果。"""

    def summarize(
        self,
        scan_result: Mapping[str, JsonValue],
        *,
        owner_id: str,
        run_id: str,
    ) -> str:
        """对持久化扫描结果做只读总结。"""

    def activate_reminder(
        self,
        *,
        owner_id: str,
        plan_id: str,
        interaction_id: str,
        idempotency_key: str,
    ) -> CardEnvelope:
        """激活一个仅用于复核的提醒。"""


class ResearchToPlanJourney(ConversationJourney):
    """负责研究、扫描、计划、提醒与复盘闭环的会话旅程。

    Contract:
        - 研究结果必须先持久化，再允许人工复核与后续计划生成。
        - LLM 只能总结扫描结果，不参与价格预测、评分或排序。
        - 该旅程生成的所有计划、提醒与复盘都必须保留来源关系。

    Implemented by:
        组合根装配了 ``ResearchJourneyBackend`` 后注册到 runtime 的 research 插件。
    """

    def __init__(
        self,
        *,
        backend: ResearchJourneyBackend,
        planning: PlanningService,
        hitl_service: DefaultHitlService,
        presenter: PlanningCardPresenter | None = None,
    ) -> None:
        self._backend = backend
        self._planning = planning
        self._hitl = hitl_service
        self._presenter = presenter or PlanningCardPresenter()

    @property
    def journey_ids(self) -> tuple[str, ...]:
        """返回本旅程负责的启动 ID。"""

        return (JOURNEY_RESEARCH_TO_PLAN,)

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回本旅程负责恢复的 subject type。"""

        return (
            _SUBJECT_SECURITY_CHOICE,
            _SUBJECT_SCAN_REVIEW,
            _SUBJECT_PLAN_FORM,
            _SUBJECT_PLAN_APPROVAL,
            _SUBJECT_REMINDER_APPROVAL,
            _SUBJECT_PLAN_REVIEW,
        )

    def start(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        """启动 research -> plan 流程。"""

        symbol = runtime.required_entity(context.classification, "symbol")
        candidates = self._backend.resolve(
            symbol,
            owner_id=context.owner_id,
            run_id=context.run_id,
        )
        if not candidates:
            notice = runtime.create_unsupported_notice(
                reference_id=context.run_id,
                unsupported_kind="security_not_found",
                message="无法解析为受支持的美股证券, 请补充交易所与代码。",
                source_type="research_request",
            )
            runtime.publish_card(
                context.owner_id,
                context.thread_id,
                context.run_id,
                notice,
                "card.failed",
            )
            return ConversationRunResult(
                context.run_id,
                context.thread_id,
                "unsupported",
                card=notice,
            )
        if len(candidates) > 1:
            interaction = self._create_security_choice(
                context.owner_id,
                context.thread_id,
                context.run_id,
                candidates,
            )
            card = runtime.publish_interaction(interaction, "card.created")
            return ConversationRunResult(
                context.run_id,
                context.thread_id,
                "waiting_for_human",
                interaction.interaction_id,
                card,
            )
        card = self._prepare_research_journey(
            owner_id=context.owner_id,
            thread_id=context.thread_id,
            run_id=context.run_id,
            security_id=candidates[0].security_id,
            runtime=runtime,
        )
        return ConversationRunResult(
            context.run_id,
            context.thread_id,
            "waiting_for_human",
            card.source.source_id,
            card,
        )

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope | None:
        """从 research 旅程的暂停点恢复执行。"""

        if interaction.subject_type == _SUBJECT_SECURITY_CHOICE:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_security_choice(interaction, runtime)
        if interaction.subject_type == _SUBJECT_SCAN_REVIEW:
            return self._handle_scan_review(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_FORM:
            runtime.publish_interaction(interaction, "card.resolved")
            return self._handle_plan_form(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_APPROVAL:
            return self._handle_plan_approval(interaction, runtime)
        if interaction.subject_type == _SUBJECT_REMINDER_APPROVAL:
            return self._handle_reminder_approval(interaction, runtime)
        if interaction.subject_type == _SUBJECT_PLAN_REVIEW:
            return self._handle_plan_review(interaction, runtime)
        return None

    def _handle_security_choice(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        selected = _required_text(interaction.response or {}, "selected_security")
        return self._prepare_research_journey(
            owner_id=interaction.owner_id,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
            security_id=selected,
            runtime=runtime,
        )

    def _prepare_research_journey(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        security_id: str,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        result = self._backend.prepare(security_id, owner_id=owner_id, run_id=run_id)
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.research_card,
            "card.created",
            artifact=True,
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_progress_started,
            "card.created",
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_progress_completed,
            "card.resolved",
        )
        runtime.publish_card(
            owner_id,
            thread_id,
            run_id,
            result.scan_result_card,
            "card.created",
            artifact=True,
        )
        runtime.save_run_context(
            owner_id=owner_id,
            run_id=run_id,
            thread_id=thread_id,
            payload={
                "security_id": result.security_id,
                "scan_result": result.scan_result_card.to_mapping(),
                "plan_values": dict(result.plan_values),
            },
        )
        review = self._create_scan_review(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            scan_result=result.scan_result_card,
        )
        return runtime.publish_interaction(review, "card.created")

    def _handle_scan_review(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        journey = runtime.require_run_context(interaction.owner_id, interaction.run_id)
        scan_result = journey["scan_result"]
        plan_values = journey["plan_values"]
        if not isinstance(scan_result, Mapping) or not isinstance(plan_values, Mapping):
            raise RuntimeError("research run context 格式无效")
        summary = self._backend.summarize(
            scan_result,
            owner_id=interaction.owner_id,
            run_id=interaction.run_id,
        )
        plan = self._planning.create_draft(
            _journey_plan_request(
                plan_values,
                plan_id=str(uuid4()),
                owner_id=interaction.owner_id,
                security_id=_required_text(journey, "security_id"),
                run_id=interaction.run_id,
                summary=summary,
            ),
            idempotency_key=f"{interaction.interaction_id}:research-plan-draft",
        )
        approval = self._create_plan_approval(
            plan,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
        )
        return runtime.publish_interaction(approval, "card.created")

    def _handle_plan_form(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        current = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        revised = self._planning.revise_draft(
            _request_from_values(
                interaction.response or {},
                owner_id=interaction.owner_id,
                plan_id=current.plan_id,
                security_id=current.security_id,
                run_id=interaction.run_id,
                source_references=current.source_references,
            ),
            expected_version=current.version,
            idempotency_key=f"{interaction.interaction_id}:revise-draft",
        )
        approval = self._create_plan_approval(
            revised,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
        )
        return runtime.publish_interaction(approval, "card.created")

    def _handle_plan_approval(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        current = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        if interaction.resolution == "edit":
            superseded = replace(
                _interaction_card(interaction),
                state="superseded",
                actions=(),
                payload_hash="",
            )
            runtime.publish_card(
                interaction.owner_id,
                interaction.thread_id,
                interaction.run_id,
                superseded,
                "card.superseded",
            )
            revised = self._planning.revise_draft(
                PlanDraftRequest(
                    plan_id=current.plan_id,
                    owner_id=current.owner_id,
                    security_id=current.security_id,
                    direction=current.direction,
                    created_at=datetime.now(UTC),
                    source_references=current.source_references,
                    horizon=current.horizon,
                    entry_condition=current.entry_condition,
                    invalidation_condition=current.invalidation_condition,
                    target=current.target,
                    position_notes=current.position_notes,
                    risk_notes=current.risk_notes,
                    field_sources=current.field_sources,
                ),
                expected_version=current.version,
                idempotency_key=f"{interaction.interaction_id}:edit-draft",
            )
            form = self._create_plan_form(
                revised,
                thread_id=interaction.thread_id,
                run_id=interaction.run_id,
            )
            return runtime.publish_interaction(form, "card.created")

        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        active = self._planning.activate(
            owner_id=current.owner_id,
            plan_id=current.plan_id,
            expected_version=current.version,
            actor_id=interaction.owner_id,
            approved=True,
            approved_payload_hash=current.approval_payload_hash,
            approval_interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:activate",
            occurred_at=datetime.now(UTC),
        )
        artifact = self._presenter.plan_artifact(active)
        published = runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            artifact,
            "card.created",
            artifact=True,
        )
        reminder = self._create_reminder_approval(active, interaction)
        runtime.publish_interaction(reminder, "card.created")
        return published

    def _handle_reminder_approval(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        reminder = self._backend.activate_reminder(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
            interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:activate-reminder",
        )
        published = runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            reminder,
            "card.created",
            artifact=True,
        )
        plan = self._planning.get_plan(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
        )
        review = self._create_plan_review(plan, interaction)
        runtime.publish_interaction(review, "card.created")
        return published

    def _handle_plan_review(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope:
        runtime.publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        result = self._planning.record_review(
            owner_id=interaction.owner_id,
            review_id=str(uuid4()),
            subject_type="plan",
            subject_id=interaction.subject_id,
            subject_version=interaction.subject_version,
            outcome=ReviewOutcome.USEFUL,
            annotations={"note": "用户确认研究、扫描、计划与提醒闭环"},
            lineage=(),
            feedback_destinations=("future_strategy_draft", "future_training_data"),
            actor_id=interaction.owner_id,
            approval_interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:record-review",
            created_at=datetime.now(UTC),
        )
        runtime.save_resource(
            owner_id=interaction.owner_id,
            resource_name="reviews",
            resource_id=result.review.review_id,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
            payload=review_payload(result),
        )
        reviewed = result.reviewed_plan
        if reviewed is None:  # pragma: no cover
            raise RuntimeError("计划复盘没有返回 reviewed plan")
        return runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            self._presenter.plan_artifact(reviewed),
            "card.created",
            artifact=True,
        )

    def _create_security_choice(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        candidates: tuple[SecurityCandidate, ...],
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "请选择具体美股证券",
            "description": "同一代码对应多个美国上市标的, 需要先澄清。",
            "text_fallback": "请选择具体美股证券。",
        }
        options: list[JsonValue] = [
            {
                "key": item.security_id,
                "label": item.label,
                "description": item.security_id,
                "disabled": False,
            }
            for item in candidates
        ]
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.CLARIFICATION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_SECURITY_CHOICE,
                run_id,
                1,
                _payload_hash(payload),
                {
                    "type": "object",
                    "properties": {
                        "selected_security": {
                            "type": "string",
                            "title": "候选证券",
                            "enum": [item.security_id for item in candidates],
                            "x-options": options,
                        }
                    },
                    "required": ["selected_security"],
                    "additionalProperties": False,
                },
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_scan_review(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        scan_result: CardEnvelope,
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "请复核量化扫描结论",
            "description": "确认后才会让 LLM 总结持久化结果并生成计划草稿。",
            "findings": [
                {
                    "label": "扫描候选",
                    "detail": scan_result.text_fallback,
                    "severity": "medium",
                }
            ],
            "text_fallback": "请复核量化扫描结论。",
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.REVIEW,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_SCAN_REVIEW,
                scan_result.source.source_id,
                scan_result.source.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_plan_form(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "补充研究生成的美股交易计划",
            "description": "系统不会下单。请人工复核并补齐周期、入场、失效、目标、仓位和风险。",
            "text_fallback": "请补充研究生成的交易计划字段。",
        }
        properties: dict[str, JsonValue] = {
            "direction": {
                "type": "string",
                "title": "方向或逻辑",
                "minLength": 1,
                "default": plan.direction,
            },
            "horizon": {
                "type": "string",
                "title": "计划周期",
                "minLength": 1,
                "default": plan.horizon,
            },
            "entry_condition": {
                "type": "string",
                "title": "入场条件",
                "minLength": 1,
                "default": plan.entry_condition,
            },
            "invalidation_condition": {
                "type": "string",
                "title": "失效或止损条件",
                "minLength": 1,
                "default": plan.invalidation_condition,
            },
            "target": {
                "type": "string",
                "title": "目标条件",
                "minLength": 1,
                "default": plan.target,
            },
            "position_notes": {
                "type": "string",
                "title": "仓位备注",
                "minLength": 1,
                "default": plan.position_notes,
            },
            "risk_notes": {
                "type": "string",
                "title": "风险说明",
                "minLength": 1,
                "default": plan.risk_notes,
            },
        }
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.EXCEPTION_RESOLUTION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_PLAN_FORM,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                schema,
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_plan_approval(
        self,
        plan: TradingPlan,
        *,
        thread_id: str,
        run_id: str,
    ) -> HumanInteraction:
        preview = self._presenter.plan_approval(plan)
        payload: dict[str, JsonValue] = {
            "title": preview.data["title"],
            "description": preview.data["description"],
            "summary": preview.data["summary"],
            "facts": preview.data["facts"],
            "provenance": preview.data["provenance"],
            "text_fallback": preview.text_fallback,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.APPROVAL,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                _SUBJECT_PLAN_APPROVAL,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_reminder_approval(
        self,
        plan: TradingPlan,
        source: HumanInteraction,
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "批准启用计划复核提醒",
            "description": "提醒只表示条件观察与通知, 不表示下单或成交。",
            "summary": f"为计划 {plan.plan_id} 启用应用内定时复核提醒。",
            "facts": [
                {"label": "计划", "detail": plan.plan_id, "severity": "low"},
                {"label": "渠道", "detail": "in_app", "severity": "low"},
            ],
            "text_fallback": "请确认启用计划复核提醒。",
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.APPROVAL,
                InteractionStatus.PENDING,
                payload,
                1,
                source.thread_id,
                source.run_id,
                _SUBJECT_REMINDER_APPROVAL,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_plan_review(self, plan: TradingPlan, source: HumanInteraction) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "完成本次计划复盘",
            "description": "复盘只写入未来策略草稿或训练数据, 不修改历史版本。",
            "findings": [
                {
                    "label": "闭环状态",
                    "detail": "研究、扫描、计划和提醒均已保留来源关系。",
                    "severity": "low",
                }
            ],
            "text_fallback": "请确认完成本次计划复盘。",
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                plan.owner_id,
                InteractionType.REVIEW,
                InteractionStatus.PENDING,
                payload,
                1,
                source.thread_id,
                source.run_id,
                _SUBJECT_PLAN_REVIEW,
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )


def _interaction_card(interaction: HumanInteraction) -> CardEnvelope:
    """把 HITL 聚合投影为统一 Card。"""

    from trade_agent.core.presentation.projection import HitlCardPresenter

    projected = HitlCardPresenter().present(interaction)
    return replace(projected, revision=interaction.version)


def _required_text(values: Mapping[str, JsonValue], field: str) -> str:
    """读取一个必须存在且非空的文本字段。"""

    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"计划字段缺少 {field}")
    return value.strip()


def _request_from_values(
    values: Mapping[str, JsonValue],
    *,
    owner_id: str,
    plan_id: str,
    security_id: str,
    run_id: str,
    source_references: tuple[PlanLineage, ...],
) -> PlanDraftRequest:
    """把研究流程中的人工修订值转换为 Planning capability 输入。"""

    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=source_references,
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=_required_text(values, "risk_notes"),
        field_sources={
            "direction": "人工修订",
            "horizon": "人工修订",
            "entry_condition": "人工修订",
            "invalidation_condition": "人工修订",
            "target": "人工修订",
            "position_notes": "人工修订",
            "risk_notes": "人工修订",
        },
    )


def _journey_plan_request(
    values: Mapping[str, JsonValue],
    *,
    plan_id: str,
    owner_id: str,
    security_id: str,
    run_id: str,
    summary: str,
) -> PlanDraftRequest:
    """使用扫描 lineage 和 LLM 摘要构造研究旅程中的计划草稿。"""

    scan_result_id = _required_text(values, "scan_result_id")
    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=(
            PlanLineage(
                "scan_result",
                scan_result_id,
                1,
                evidence_ids=("e-quote", "e-fundamental"),
                strategy_id="strategy-1",
                strategy_version=1,
                model_version_id="model-approved",
            ),
        ),
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=f"{_required_text(values, 'risk_notes')} LLM 摘要: {summary}",
        field_sources={
            "direction": "用户请求",
            "horizon": "strategy_version",
            "entry_condition": "research_artifact",
            "invalidation_condition": "research_artifact",
            "target": "scan_result",
            "position_notes": "用户策略",
            "risk_notes": "research_summary",
        },
    )


def _payload_hash(payload: Mapping[str, JsonValue]) -> str:
    """计算一个 HITL payload 的稳定哈希。"""

    from trade_agent.adapters.sqlite.json_support import payload_hash

    return payload_hash(payload)


__all__ = [
    "ResearchJourneyBackend",
    "ResearchJourneyResult",
    "ResearchToPlanJourney",
    "SecurityCandidate",
]
