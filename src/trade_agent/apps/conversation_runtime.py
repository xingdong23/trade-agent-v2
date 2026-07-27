"""真实会话入口: 把 planning 对话推进为持久化 HITL、Card 与事件。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import (
    SQLiteAggregateRepository,
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteThreadCheckpointer,
)
from trade_agent.capabilities.planning.application import PlanDraftRequest, PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage, ReviewOutcome, TradingPlan
from trade_agent.core.events import RunEvent
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.presentation.projection import HitlCardPresenter
from trade_agent.core.runtime import AgentState, Intent

_SYMBOL_PATTERN = re.compile(r"\b([A-Za-z]{1,5})\b")
_PLAN_FIELDS = (
    "horizon",
    "entry_condition",
    "invalidation_condition",
    "target",
    "position_notes",
    "risk_notes",
)


class GraphInvoker(Protocol):
    def invoke(self, input: AgentState) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class SecurityCandidate:
    security_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ResearchJourneyResult:
    security_id: str
    research_card: CardEnvelope
    scan_progress_started: CardEnvelope
    scan_progress_completed: CardEnvelope
    scan_result_card: CardEnvelope
    plan_values: Mapping[str, JsonValue]


class ResearchJourneyBackend(Protocol):
    """外部数据与量化能力的可替换应用边界。"""

    def resolve(
        self, symbol: str, *, owner_id: str, run_id: str
    ) -> tuple[SecurityCandidate, ...]: ...

    def prepare(self, security_id: str, *, owner_id: str, run_id: str) -> ResearchJourneyResult: ...

    def summarize(
        self, scan_result: Mapping[str, JsonValue], *, owner_id: str, run_id: str
    ) -> str: ...

    def activate_reminder(
        self,
        *,
        owner_id: str,
        plan_id: str,
        interaction_id: str,
        idempotency_key: str,
    ) -> CardEnvelope: ...


@dataclass(frozen=True, slots=True)
class ConversationRunResult:
    run_id: str
    thread_id: str
    status: str
    pending_interaction_id: str | None = None
    card: CardEnvelope | None = None


class ConversationRunService:
    """首条 production vertical slice, 明确限制为 planning/HITL 工作流。"""

    def __init__(
        self,
        *,
        graph: GraphInvoker,
        database: SQLiteDatabase,
        checkpointer: SQLiteThreadCheckpointer,
        event_store: SQLiteEventStore,
        hitl_service: DefaultHitlService,
        planning: PlanningService | None = None,
        research_journey: ResearchJourneyBackend | None = None,
        tracer: StructuredTracer | None = None,
    ) -> None:
        self._graph = graph
        self._checkpointer = checkpointer
        self._events = event_store
        self._hitl = hitl_service
        self._planning = planning or PlanningService()
        self._research_journey = research_journey
        self._tracer = tracer or StructuredTracer()
        self._cards = SQLiteAggregateRepository(database, "cards")
        self._artifacts = SQLiteAggregateRepository(database, "artifacts")
        self._journeys = SQLiteAggregateRepository(database, "run_contexts")
        self._reviews = SQLiteAggregateRepository(database, "reviews")
        self._handled_interactions: dict[str, CardEnvelope] = {}

    @property
    def tracer(self) -> StructuredTracer:
        return self._tracer

    def start_run(
        self,
        *,
        owner_id: str,
        thread_id: str,
        message: str,
        correlation_id: str,
    ) -> ConversationRunResult:
        run_id = str(uuid4())
        self._checkpointer.bind_thread(owner_id=owner_id, thread_id=thread_id)
        self._events.start_run(owner_id=owner_id, run_id=run_id, thread_id=thread_id)
        self._append_event(
            owner_id,
            run_id,
            "run.started",
            {"thread_id": thread_id, "intent": "planning"},
        )
        self._graph.invoke(
            AgentState(
                user_id=owner_id,
                thread_id=thread_id,
                run_id=run_id,
                message=message,
                intent=Intent.PLANNING,
            )
        )
        self._tracer.emit(
            correlation_id=correlation_id,
            event_type="conversation.routed",
            outcome="success",
            attributes={"run_id": run_id, "agent_id": "planning"},
        )

        normalized = message.strip()
        research_symbol = self._research_symbol(normalized)
        if research_symbol is not None and self._research_journey is not None:
            candidates = self._research_journey.resolve(
                research_symbol, owner_id=owner_id, run_id=run_id
            )
            if not candidates:
                notice = PlanningCardPresenter().unsupported(
                    reference_id=run_id,
                    unsupported_kind="security_not_found",
                    message="无法解析为受支持的美股证券, 请补充交易所与代码。",
                )
                self._publish_card(owner_id, thread_id, run_id, notice, "card.failed")
                return ConversationRunResult(run_id, thread_id, "unsupported", card=notice)
            if len(candidates) > 1:
                interaction = self._create_security_choice(owner_id, thread_id, run_id, candidates)
                card = self._publish_interaction(interaction, "card.created")
                return ConversationRunResult(
                    run_id, thread_id, "waiting_for_human", interaction.interaction_id, card
                )
            card = self._prepare_research_journey(
                owner_id=owner_id,
                thread_id=thread_id,
                run_id=run_id,
                security_id=candidates[0].security_id,
            )
            return ConversationRunResult(
                run_id, thread_id, "waiting_for_human", card.source.source_id, card
            )

        if "新增一个交易" in normalized:
            interaction = self._create_choice(owner_id, thread_id, run_id)
            card = self._publish_interaction(interaction, "card.created")
            return ConversationRunResult(
                run_id, thread_id, "waiting_for_human", interaction.interaction_id, card
            )

        symbol = self._buy_symbol(normalized)
        if symbol is not None:
            notice = PlanningCardPresenter().unsupported(
                reference_id=run_id,
                unsupported_kind="execute_trade",
                message="系统不能下单; 可以继续创建仅用于研究与决策的美股交易计划。",
            )
            self._publish_card(owner_id, thread_id, run_id, notice, "card.created")
            plan = self._planning.create_draft(
                PlanDraftRequest(
                    plan_id=str(uuid4()),
                    owner_id=owner_id,
                    security_id=f"US:NASDAQ:{symbol}",
                    direction=f"为 {symbol} 创建买入研究计划, 不执行下单",
                    created_at=datetime.now(UTC),
                    source_references=(PlanLineage("user_request", run_id, 1),),
                    field_sources={"security_id": "用户输入", "direction": "用户输入"},
                ),
                idempotency_key=f"{run_id}:create-draft",
            )
            interaction = self._create_plan_form(plan, thread_id=thread_id, run_id=run_id)
            card = self._publish_interaction(interaction, "card.created")
            return ConversationRunResult(
                run_id, thread_id, "waiting_for_human", interaction.interaction_id, card
            )

        notice = PlanningCardPresenter().unsupported(
            reference_id=run_id,
            unsupported_kind="conversation_intent",
            message="当前真实会话入口仅支持美股交易计划; 研究与扫描全链仍在装配中。",
        )
        self._publish_card(owner_id, thread_id, run_id, notice, "card.failed")
        return ConversationRunResult(run_id, thread_id, "unsupported", card=notice)

    def handle_resolved_interaction(self, interaction: HumanInteraction) -> CardEnvelope | None:
        replay = self._handled_interactions.get(interaction.interaction_id)
        if replay is not None:
            return replay
        if interaction.status is not InteractionStatus.RESOLVED:
            return None

        if interaction.subject_type == "planning_choice":
            self._publish_interaction(interaction, "card.resolved")
            card = self._handle_choice(interaction)
        elif interaction.subject_type in {"planning_request", "plan_form"}:
            self._publish_interaction(interaction, "card.resolved")
            card = self._handle_form(interaction)
        elif interaction.subject_type == "plan_approval":
            card = self._handle_approval(interaction)
        elif interaction.subject_type == "research_security_choice":
            self._publish_interaction(interaction, "card.resolved")
            card = self._handle_security_choice(interaction)
        elif interaction.subject_type == "scan_review":
            card = self._handle_scan_review(interaction)
        elif interaction.subject_type == "reminder_approval":
            card = self._handle_reminder_approval(interaction)
        elif interaction.subject_type == "plan_review":
            card = self._handle_plan_review(interaction)
        else:
            return None
        self._handled_interactions[interaction.interaction_id] = card
        return card

    def _handle_choice(self, interaction: HumanInteraction) -> CardEnvelope:
        response = interaction.response or {}
        if response.get("choice") != "create_trade_plan":
            card = PlanningCardPresenter().unsupported(
                reference_id=interaction.interaction_id,
                unsupported_kind="execute_or_record_trade",
                message="首版只支持创建交易计划, 不支持成交记录或真实下单。",
            )
            return self._publish_card(
                interaction.owner_id,
                interaction.thread_id,
                interaction.run_id,
                card,
                "card.failed",
            )
        plan_id = str(uuid4())
        next_interaction = self._create_request_form(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            plan_id,
        )
        return self._publish_interaction(next_interaction, "card.created")

    def _handle_form(self, interaction: HumanInteraction) -> CardEnvelope:
        values = interaction.response or {}
        if interaction.subject_type == "planning_request":
            symbol = _required_text(values, "symbol").upper()
            plan = self._planning.create_draft(
                _request_from_values(
                    values,
                    owner_id=interaction.owner_id,
                    plan_id=interaction.subject_id,
                    security_id=f"US:NASDAQ:{symbol}",
                    run_id=interaction.run_id,
                ),
                idempotency_key=f"{interaction.interaction_id}:create-draft",
            )
        else:
            current = self._planning.get_plan(
                owner_id=interaction.owner_id, plan_id=interaction.subject_id
            )
            plan = self._planning.revise_draft(
                _request_from_values(
                    values,
                    owner_id=interaction.owner_id,
                    plan_id=current.plan_id,
                    security_id=current.security_id,
                    run_id=interaction.run_id,
                ),
                expected_version=current.version,
                idempotency_key=f"{interaction.interaction_id}:revise-draft",
            )
        approval = self._create_plan_approval(
            plan, thread_id=interaction.thread_id, run_id=interaction.run_id
        )
        return self._publish_interaction(approval, "card.created")

    def _handle_approval(self, interaction: HumanInteraction) -> CardEnvelope:
        current = self._planning.get_plan(
            owner_id=interaction.owner_id, plan_id=interaction.subject_id
        )
        if interaction.resolution == "edit":
            superseded = replace(
                _interaction_card(interaction),
                state="superseded",
                actions=(),
                payload_hash="",
            )
            self._publish_card(
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
                revised, thread_id=interaction.thread_id, run_id=interaction.run_id
            )
            return self._publish_interaction(form, "card.created")

        self._publish_interaction(interaction, "card.resolved")
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
        artifact = PlanningCardPresenter().plan_artifact(active)
        published = self._publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            artifact,
            "card.created",
            artifact=True,
        )
        if self._journeys.get(interaction.owner_id, interaction.run_id) is not None:
            reminder = self._create_reminder_approval(active, interaction)
            self._publish_interaction(reminder, "card.created")
        return published

    def _handle_security_choice(self, interaction: HumanInteraction) -> CardEnvelope:
        selected = _required_text(interaction.response or {}, "selected_security")
        return self._prepare_research_journey(
            owner_id=interaction.owner_id,
            thread_id=interaction.thread_id,
            run_id=interaction.run_id,
            security_id=selected,
        )

    def _prepare_research_journey(
        self, *, owner_id: str, thread_id: str, run_id: str, security_id: str
    ) -> CardEnvelope:
        backend = self._research_journey
        if backend is None:  # pragma: no cover - 入口已 fail closed
            raise RuntimeError("research journey backend 未装配")
        result = backend.prepare(security_id, owner_id=owner_id, run_id=run_id)
        self._publish_card(
            owner_id, thread_id, run_id, result.research_card, "card.created", artifact=True
        )
        self._publish_card(
            owner_id, thread_id, run_id, result.scan_progress_started, "card.created"
        )
        self._publish_card(
            owner_id, thread_id, run_id, result.scan_progress_completed, "card.resolved"
        )
        self._publish_card(
            owner_id, thread_id, run_id, result.scan_result_card, "card.created", artifact=True
        )
        self._journeys.save(
            owner_id=owner_id,
            aggregate_id=run_id,
            expected_version=0,
            payload={
                "thread_id": thread_id,
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
        return self._publish_interaction(review, "card.created")

    def _handle_scan_review(self, interaction: HumanInteraction) -> CardEnvelope:
        self._publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        journey = self._required_journey(interaction.owner_id, interaction.run_id)
        scan_result = journey["scan_result"]
        plan_values = journey["plan_values"]
        if not isinstance(scan_result, Mapping) or not isinstance(plan_values, Mapping):
            raise RuntimeError("research run context 格式无效")
        backend = self._research_journey
        if backend is None:  # pragma: no cover
            raise RuntimeError("research journey backend 未装配")
        summary = backend.summarize(
            scan_result, owner_id=interaction.owner_id, run_id=interaction.run_id
        )
        plan_id = str(uuid4())
        plan = self._planning.create_draft(
            _journey_plan_request(
                plan_values,
                plan_id=plan_id,
                owner_id=interaction.owner_id,
                security_id=_required_text(journey, "security_id"),
                run_id=interaction.run_id,
                summary=summary,
            ),
            idempotency_key=f"{interaction.interaction_id}:research-plan-draft",
        )
        approval = self._create_plan_approval(
            plan, thread_id=interaction.thread_id, run_id=interaction.run_id
        )
        return self._publish_interaction(approval, "card.created")

    def _handle_reminder_approval(self, interaction: HumanInteraction) -> CardEnvelope:
        self._publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        backend = self._research_journey
        if backend is None:  # pragma: no cover
            raise RuntimeError("research journey backend 未装配")
        reminder = backend.activate_reminder(
            owner_id=interaction.owner_id,
            plan_id=interaction.subject_id,
            interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:activate-reminder",
        )
        published = self._publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            reminder,
            "card.created",
            artifact=True,
        )
        plan = self._planning.get_plan(
            owner_id=interaction.owner_id, plan_id=interaction.subject_id
        )
        review = self._create_plan_review(plan, interaction)
        self._publish_interaction(review, "card.created")
        return published

    def _handle_plan_review(self, interaction: HumanInteraction) -> CardEnvelope:
        self._publish_interaction(interaction, "card.resolved")
        if interaction.resolution != "confirm":
            return _interaction_card(interaction)
        current = self._planning.get_plan(
            owner_id=interaction.owner_id, plan_id=interaction.subject_id
        )
        result = self._planning.record_review(
            owner_id=interaction.owner_id,
            review_id=str(uuid4()),
            subject_type="plan",
            subject_id=current.plan_id,
            subject_version=current.version,
            outcome=ReviewOutcome.USEFUL,
            annotations={"note": "用户确认研究、扫描、计划与提醒闭环"},
            lineage=(),
            feedback_destinations=("future_strategy_draft", "future_training_data"),
            actor_id=interaction.owner_id,
            approval_interaction_id=interaction.interaction_id,
            idempotency_key=f"{interaction.interaction_id}:record-review",
            created_at=datetime.now(UTC),
        )
        self._reviews.save(
            owner_id=interaction.owner_id,
            aggregate_id=result.review.review_id,
            expected_version=0,
            payload={
                "thread_id": interaction.thread_id,
                "run_id": interaction.run_id,
                "subject_id": result.review.subject_id,
                "subject_version": result.review.subject_version,
                "outcome": result.review.outcome.value,
            },
        )
        reviewed = result.reviewed_plan
        if reviewed is None:  # pragma: no cover
            raise RuntimeError("计划复盘没有返回 reviewed plan")
        return self._publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            PlanningCardPresenter().plan_artifact(reviewed),
            "card.created",
            artifact=True,
        )

    def _create_choice(self, owner_id: str, thread_id: str, run_id: str) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "请选择要新增的内容",
            "description": "首版只支持创建美股交易计划, 不支持成交记录或真实下单。",
            "text_fallback": "请选择创建交易计划; 其他交易操作暂不支持。",
        }
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
                "planning_choice",
                run_id,
                1,
                _payload_hash(payload),
                {
                    "type": "object",
                    "properties": {
                        "choice": {
                            "type": "string",
                            "title": "操作类型",
                            "enum": [
                                "create_trade_plan",
                                "record_historical_trade",
                                "execute_trade",
                            ],
                            "x-options": [
                                {
                                    "key": "create_trade_plan",
                                    "label": "创建交易计划",
                                    "description": "补充条件并在确认后激活计划。",
                                    "disabled": False,
                                },
                                {
                                    "key": "record_historical_trade",
                                    "label": "记录已发生的交易",
                                    "description": "首版暂不支持手工成交记录。",
                                    "disabled": True,
                                },
                                {
                                    "key": "execute_trade",
                                    "label": "执行真实交易",
                                    "description": "系统没有下单或账户能力。",
                                    "disabled": True,
                                },
                            ],
                        }
                    },
                    "required": ["choice"],
                    "additionalProperties": False,
                },
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
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
                "research_security_choice",
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
                "scan_review",
                scan_result.source.source_id,
                scan_result.source.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_reminder_approval(
        self, plan: TradingPlan, source: HumanInteraction
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
                "reminder_approval",
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
                "plan_review",
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_request_form(
        self, owner_id: str, thread_id: str, run_id: str, plan_id: str
    ) -> HumanInteraction:
        return self._create_form_interaction(
            owner_id=owner_id,
            thread_id=thread_id,
            run_id=run_id,
            subject_type="planning_request",
            plan_id=plan_id,
            plan_version=1,
            symbol=None,
            direction="创建买入研究计划, 不执行下单",
            defaults={},
        )

    def _create_plan_form(
        self, plan: TradingPlan, *, thread_id: str, run_id: str
    ) -> HumanInteraction:
        return self._create_form_interaction(
            owner_id=plan.owner_id,
            thread_id=thread_id,
            run_id=run_id,
            subject_type="plan_form",
            plan_id=plan.plan_id,
            plan_version=plan.version,
            symbol=plan.security_id.rsplit(":", 1)[-1],
            direction=plan.direction,
            defaults={field: getattr(plan, field) for field in _PLAN_FIELDS},
        )

    def _create_form_interaction(
        self,
        *,
        owner_id: str,
        thread_id: str,
        run_id: str,
        subject_type: str,
        plan_id: str,
        plan_version: int,
        symbol: str | None,
        direction: str,
        defaults: Mapping[str, str | None],
    ) -> HumanInteraction:
        payload: dict[str, JsonValue] = {
            "title": "补充美股交易计划",
            "description": "系统不会下单。请一次补齐证券、周期、入场、失效、目标、仓位和风险。",
            "text_fallback": "请补充完整的美股交易计划字段。",
        }
        properties: dict[str, JsonValue] = {
            "symbol": {
                "type": "string",
                "title": "美股代码",
                "pattern": "^[A-Za-z]{1,5}$",
                "default": symbol,
                "readOnly": symbol is not None,
            },
            "direction": {
                "type": "string",
                "title": "方向或逻辑",
                "minLength": 1,
                "default": direction,
            },
        }
        labels = {
            "horizon": "计划周期",
            "entry_condition": "入场条件",
            "invalidation_condition": "失效或止损条件",
            "target": "目标条件",
            "position_notes": "仓位备注",
            "risk_notes": "风险说明",
        }
        for field, label in labels.items():
            properties[field] = {
                "type": "string",
                "title": label,
                "minLength": 1,
                "maxLength": 1000,
                "default": defaults.get(field),
                "format": "textarea" if field != "horizon" else "text",
            }
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": properties,
            "required": ["symbol", "direction", *_PLAN_FIELDS],
            "additionalProperties": False,
        }
        return self._hitl.create(
            HumanInteraction(
                str(uuid4()),
                owner_id,
                InteractionType.EXCEPTION_RESOLUTION,
                InteractionStatus.PENDING,
                payload,
                1,
                thread_id,
                run_id,
                subject_type,
                plan_id,
                plan_version,
                _payload_hash(payload),
                schema,
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _create_plan_approval(
        self, plan: TradingPlan, *, thread_id: str, run_id: str
    ) -> HumanInteraction:
        preview = PlanningCardPresenter().plan_approval(plan)
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
                "plan_approval",
                plan.plan_id,
                plan.version,
                _payload_hash(payload),
                {"type": "object", "properties": {}, "additionalProperties": False},
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(hours=24),
            )
        )

    def _publish_interaction(self, interaction: HumanInteraction, event_type: str) -> CardEnvelope:
        return self._publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            _interaction_card(interaction),
            event_type,
        )

    def _publish_card(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        card: CardEnvelope,
        event_type: str,
        *,
        artifact: bool = False,
    ) -> CardEnvelope:
        repository = self._artifacts if artifact else self._cards
        existing = repository.get(owner_id, card.card_id)
        expected_version = 0 if existing is None else existing.version
        repository.save(
            owner_id=owner_id,
            aggregate_id=card.card_id,
            expected_version=expected_version,
            payload={"thread_id": thread_id, "run_id": run_id, "card": card.to_mapping()},
        )
        self._append_event(
            owner_id,
            run_id,
            event_type,
            {"card": card.to_mapping(), "card_id": card.card_id, "revision": card.revision},
        )
        self._tracer.emit(
            correlation_id=run_id,
            event_type=event_type,
            outcome=card.state,
            attributes={"card_id": card.card_id, "kind": card.kind, "revision": card.revision},
        )
        return card

    def _append_event(
        self, owner_id: str, run_id: str, event_type: str, payload: Mapping[str, JsonValue]
    ) -> None:
        previous = self._events.replay(owner_id=owner_id, run_id=run_id, after_sequence=0)
        self._events.append(
            owner_id=owner_id,
            event=RunEvent(
                str(uuid4()),
                run_id,
                len(previous) + 1,
                event_type,
                payload,
                datetime.now(UTC),
            ),
        )

    def _required_journey(self, owner_id: str, run_id: str) -> Mapping[str, JsonValue]:
        journey = self._journeys.get(owner_id, run_id)
        if journey is None:
            raise RuntimeError("research run context 不存在")
        return journey.payload

    @staticmethod
    def _buy_symbol(message: str) -> str | None:
        if "买" not in message and "buy" not in message.casefold():
            return None
        candidates = [match.group(1).upper() for match in _SYMBOL_PATTERN.finditer(message)]
        return candidates[-1] if candidates else None

    @staticmethod
    def _research_symbol(message: str) -> str | None:
        if not any(keyword in message for keyword in ("研究", "分析", "扫描")):
            return None
        candidates = [match.group(1).upper() for match in _SYMBOL_PATTERN.finditer(message)]
        return candidates[-1] if candidates else None


def _interaction_card(interaction: HumanInteraction) -> CardEnvelope:
    projected = HitlCardPresenter().present(interaction)
    return replace(projected, revision=interaction.version)


def _required_text(values: Mapping[str, JsonValue], field: str) -> str:
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
) -> PlanDraftRequest:
    return PlanDraftRequest(
        plan_id=plan_id,
        owner_id=owner_id,
        security_id=security_id,
        direction=_required_text(values, "direction"),
        created_at=datetime.now(UTC),
        source_references=(PlanLineage("user_request", run_id, 1),),
        horizon=_required_text(values, "horizon"),
        entry_condition=_required_text(values, "entry_condition"),
        invalidation_condition=_required_text(values, "invalidation_condition"),
        target=_required_text(values, "target"),
        position_notes=_required_text(values, "position_notes"),
        risk_notes=_required_text(values, "risk_notes"),
        field_sources={field: "用户输入" for field in ("direction", *_PLAN_FIELDS)},
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
    from trade_agent.adapters.sqlite.json_support import payload_hash

    return payload_hash(payload)


__all__ = [
    "ConversationRunResult",
    "ConversationRunService",
    "ResearchJourneyBackend",
    "ResearchJourneyResult",
    "SecurityCandidate",
]
