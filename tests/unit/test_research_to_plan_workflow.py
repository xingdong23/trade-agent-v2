from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from trade_agent.apps.workflows.contracts import WorkflowRuntime, WorkflowStartContext
from trade_agent.apps.workflows.planning import planning_presenter_config_from_settings
from trade_agent.apps.workflows.research_to_plan import (
    WORKFLOW_RESEARCH_TO_PLAN,
    PlanApprovalPayloadStrategy,
    PlanLineageConfig,
    PlanReviewConfig,
    ReminderApprovalConfig,
    ResearchToPlanWorkflow,
    ResearchToPlanWorkflowConfig,
    ResearchWorkflowBackend,
    ResearchWorkflowResult,
    ReviewFeedbackDestinationOption,
    ScanReviewConfig,
    SecurityCandidate,
    SecurityClarificationConfig,
    _interaction_card,
)
from trade_agent.capabilities.contracts import CapabilityResult
from trade_agent.capabilities.market_research.cards import MarketResearchCardPresenter
from trade_agent.capabilities.planning.application import PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.quantitative.cards import QuantitativeCardPresenter
from trade_agent.capabilities.reminder.cards import ReminderCardPresenter
from trade_agent.core.config import PlanningWorkflowSettings
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
)
from trade_agent.core.hitl.contracts import HitlRepository
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import Intent, IntentClassification

NOW = datetime(2026, 7, 27, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class SavedResource:
    owner_id: str
    resource_name: str
    resource_id: str
    thread_id: str
    run_id: str
    payload: Mapping[str, JsonValue]


class InMemoryHitlRepository(HitlRepository):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], HumanInteraction] = {}

    def create(self, interaction: HumanInteraction) -> HumanInteraction:
        self._items[(interaction.owner_id, interaction.interaction_id)] = interaction
        return interaction

    def get(self, owner_id: str, interaction_id: str) -> HumanInteraction | None:
        return self._items.get((owner_id, interaction_id))

    def list_pending(self, owner_id: str) -> tuple[HumanInteraction, ...]:
        return tuple(
            item
            for key, item in self._items.items()
            if key[0] == owner_id and item.status is InteractionStatus.PENDING
        )

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
    ) -> HumanInteraction:
        current = self._require(owner_id, interaction_id)
        if current.version != expected_version:
            raise ValueError("interaction version 冲突")
        if current.subject_version != subject_version:
            raise ValueError("subject version 冲突")
        if current.payload_hash != payload_hash:
            raise ValueError("payload hash 冲突")
        resolved = replace(
            current,
            status=InteractionStatus.RESOLVED,
            version=current.version + 1,
            response=dict(response),
            resolved_by=actor_id,
            resolution=resolution,
            resolved_at=NOW,
        )
        self._items[(owner_id, interaction_id)] = resolved
        return resolved

    def transition(
        self,
        *,
        owner_id: str,
        interaction_id: str,
        expected_version: int,
        status: InteractionStatus,
        actor_id: str | None = None,
        resolution: str | None = None,
    ) -> HumanInteraction:
        current = self._require(owner_id, interaction_id)
        if current.version != expected_version:
            raise ValueError("interaction version 冲突")
        updated = replace(
            current,
            status=status,
            version=current.version + 1,
            resolved_by=actor_id,
            resolution=resolution,
            resolved_at=NOW if actor_id or resolution else current.resolved_at,
        )
        self._items[(owner_id, interaction_id)] = updated
        return updated

    def _require(self, owner_id: str, interaction_id: str) -> HumanInteraction:
        current = self.get(owner_id, interaction_id)
        if current is None:
            raise ValueError("interaction 不存在")
        return current


class RecordingRuntime(WorkflowRuntime):
    def __init__(self) -> None:
        self.interactions: list[HumanInteraction] = []
        self.cards: list[tuple[str, bool, object]] = []
        self.run_contexts: dict[tuple[str, str], Mapping[str, JsonValue]] = {}
        self.saved_resources: list[SavedResource] = []

    def publish_interaction(self, interaction: HumanInteraction, event_type: str) -> CardEnvelope:
        self.interactions.append(interaction)
        self.cards.append((event_type, False, interaction))
        return _interaction_card(interaction)

    def publish_card(
        self,
        owner_id: str,
        thread_id: str,
        run_id: str,
        card: CardEnvelope,
        event_type: str,
        *,
        artifact: bool = False,
    ) -> CardEnvelope:
        del owner_id, thread_id, run_id
        self.cards.append((event_type, artifact, card))
        return card

    def create_unsupported_notice(
        self,
        *,
        reference_id: str,
        unsupported_kind: str,
        message: str,
        source_type: str = "conversation_request",
        revision: int = 1,
    ) -> CardEnvelope:
        raise AssertionError(
            f"unexpected unsupported notice: {reference_id} {unsupported_kind} {message} "
            f"{source_type} rev={revision}"
        )

    def save_run_context(
        self,
        *,
        owner_id: str,
        run_id: str,
        thread_id: str,
        payload: Mapping[str, JsonValue],
        expected_version: int = 0,
    ) -> None:
        del thread_id, expected_version
        self.run_contexts[(owner_id, run_id)] = dict(payload)

    def save_resource(
        self,
        *,
        owner_id: str,
        resource_name: str,
        resource_id: str,
        thread_id: str,
        run_id: str,
        payload: Mapping[str, JsonValue],
        expected_version: int = 0,
    ) -> None:
        del expected_version
        self.saved_resources.append(
            SavedResource(owner_id, resource_name, resource_id, thread_id, run_id, dict(payload))
        )

    def require_run_context(self, owner_id: str, run_id: str) -> Mapping[str, JsonValue]:
        return self.run_contexts[(owner_id, run_id)]

    def required_entity(self, classification: IntentClassification, name: str) -> str:
        value = classification.entity(name)
        if value is None:
            raise ValueError(f"missing entity {name}")
        return value


class ConfigurableResearchBackend(ResearchWorkflowBackend):
    def __init__(self) -> None:
        self.activated_channels: list[str] = []

    def resolve(self, symbol: str, *, owner_id: str, run_id: str) -> tuple[SecurityCandidate, ...]:
        del owner_id, run_id
        return (
            SecurityCandidate(f"US:NASDAQ:{symbol}", f"{symbol} Corporation (NASDAQ)"),
            SecurityCandidate(f"US:NYSE:{symbol}", f"{symbol} Depositary (NYSE)"),
        )

    def prepare(self, security_id: str, *, owner_id: str, run_id: str) -> ResearchWorkflowResult:
        del owner_id, run_id
        research_card = MarketResearchCardPresenter().present(
            CapabilityResult(
                "research-1",
                1,
                {
                    "card_type": "research_artifact",
                    "title": f"{security_id} 研究",
                    "summary": "研究结论已固定。",
                    "sections": [
                        {
                            "title": "核心结论",
                            "content": "趋势仍需人工确认。",
                            "kind": "analysis",
                        }
                    ],
                    "evidence": [
                        {
                            "provider": "provider-a",
                            "source_reference": "quote:NVDA",
                            "evidence_id": "e-1",
                            "freshness": "fresh",
                            "retrieved_at": NOW.isoformat(),
                        }
                    ],
                },
            )
        )
        quantitative = QuantitativeCardPresenter()
        progress_started = quantitative.present(
            CapabilityResult(
                "scan-1",
                1,
                {
                    "card_type": "scan_progress",
                    "status": "running",
                    "completed": 0,
                    "total": 1,
                    "current_step": security_id,
                    "eta_seconds": 3,
                },
            )
        )
        progress_completed = quantitative.present(
            CapabilityResult(
                "scan-1",
                2,
                {
                    "card_type": "scan_progress",
                    "status": "completed",
                    "completed": 1,
                    "total": 1,
                    "current_step": security_id,
                    "eta_seconds": 0,
                },
            )
        )
        scan_card = quantitative.present(
            CapabilityResult(
                "scan-result-1",
                1,
                {
                    "card_type": "scan_result",
                    "security_id": security_id,
                    "status": "match",
                    "score": 0.88,
                    "probability": 0.73,
                    "matched_conditions": ["c-1"],
                    "exclusions": [],
                    "risks": ["earnings"],
                    "gaps": [],
                    "evidence_ids": ["e-1"],
                    "model_version_id": "model-1",
                    "feature_snapshot_id": "features-1",
                },
            )
        )
        return ResearchWorkflowResult(
            security_id=security_id,
            research_card=research_card,
            scan_progress_started=progress_started,
            scan_progress_completed=progress_completed,
            scan_result_card=scan_card,
            plan_values={
                "scan_result_id": scan_card.source.source_id,
                "scan_result_version": scan_card.source.version,
                "evidence_ids": ["e-1"],
                "strategy_id": "strategy-1",
                "strategy_version": 2,
                "model_version_id": "model-1",
                "direction": "逢回撤观察后再买入",
                "horizon": "10 个交易日",
                "entry_condition": "回到支撑位后确认反弹",
                "invalidation_condition": "跌破支撑位",
                "target": "触及目标区间后复核",
                "position_notes": "分两次建仓",
                "risk_notes": "财报日前降低暴露。",
            },
        )

    def summarize(
        self,
        scan_result: Mapping[str, JsonValue],
        *,
        owner_id: str,
        run_id: str,
    ) -> str:
        del owner_id, run_id
        assert scan_result["kind"] == "artifact.scan_result"
        return "只基于持久化扫描结果的摘要"

    def activate_reminder(
        self,
        *,
        owner_id: str,
        plan_id: str,
        interaction_id: str,
        idempotency_key: str,
        notification_channel: str,
    ) -> CardEnvelope:
        del interaction_id, idempotency_key
        self.activated_channels.append(notification_channel)
        return ReminderCardPresenter().present(
            CapabilityResult(
                f"reminder:{plan_id}",
                1,
                {
                    "card_type": "reminder",
                    "reminder_id": f"reminder:{plan_id}",
                    "plan_id": plan_id,
                    "status": "active",
                    "rule_type": "scheduled_review",
                    "notification_channel": notification_channel,
                    "execution_disclaimer": f"{owner_id} 仅接收提醒，不触发下单。",
                    "condition": {"scheduled_at": (NOW + timedelta(days=5)).isoformat()},
                },
            )
        )


def _context() -> WorkflowStartContext:
    return WorkflowStartContext(
        owner_id="owner-a",
        thread_id="thread-1",
        run_id="run-1",
        classification=IntentClassification(
            intent=Intent.RESEARCH,
            workflow_id=WORKFLOW_RESEARCH_TO_PLAN,
            confidence=1.0,
            reason_code="classified",
            entities=(("symbol", "NVDA"),),
        ),
    )


def _require_mapping(value: JsonValue) -> Mapping[str, JsonValue]:
    assert isinstance(value, Mapping)
    return value


def _require_list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def test_research_to_plan_workflow_uses_injected_configuration() -> None:
    repository = InMemoryHitlRepository()
    hitl = DefaultHitlService(repository)
    backend = ConfigurableResearchBackend()
    planning = PlanningService()
    runtime = RecordingRuntime()
    config = ResearchToPlanWorkflowConfig(
        security_clarification=SecurityClarificationConfig(
            option_title="请选择上市地",
            title="需要先确认目标证券",
            description="同名证券存在多个上市地，请先明确。",
            text_fallback="请先确认目标证券。",
            unsupported_kind="test_security_not_found",
            unsupported_message="测试证券无法解析",
            unsupported_source_type="test_research_request",
        ),
        scan_review=ScanReviewConfig(
            title="人工确认扫描结论",
            description="只有人工确认后才允许进入计划生成。",
            finding_label="待确认信号",
            text_fallback="请人工确认扫描结论。",
        ),
        plan_approval=PlanApprovalPayloadStrategy(
            payload_fields=("title", "summary", "facts"),
            include_text_fallback=True,
        ),
        reminder_approval=ReminderApprovalConfig(
            title="批准发送桌面提醒",
            description="提醒只用于复核，不代表执行。",
            summary_template="为计划 {plan_id} 启用 desktop_push 复核提醒。",
            plan_fact_label="待提醒计划",
            channel_fact_label="通知渠道",
            notification_channel="desktop_push",
            text_fallback="请确认启用桌面提醒。",
        ),
        plan_review=PlanReviewConfig(
            title="提交这次复盘",
            description="复盘结果只写入训练数据目录。",
            finding_label="复盘闭环",
            finding_detail="本次复盘必须保留研究到提醒的来源链。",
            text_fallback="请提交这次复盘。",
            feedback_destinations=(
                ReviewFeedbackDestinationOption("future_training_data", "训练样本"),
            ),
            resource_name="review_records",
        ),
        plan_lineage=PlanLineageConfig(source_type="research_artifact"),
    )
    workflow = ResearchToPlanWorkflow(
        backend=backend,
        planning=planning,
        hitl_service=hitl,
        interaction_ttl_seconds=600,
        text_field_max_length=240,
        config=config,
        presenter=PlanningCardPresenter(
            planning_presenter_config_from_settings(PlanningWorkflowSettings())
        ),
    )

    started = workflow.start(_context(), runtime)
    assert started.status == "waiting_for_human"
    choice = repository.get("owner-a", started.pending_interaction_id or "")
    assert choice is not None
    assert choice.payload["title"] == "需要先确认目标证券"
    assert choice.payload["description"] == "同名证券存在多个上市地，请先明确。"
    choice_properties = _require_mapping(choice.response_schema["properties"])
    selected_security = _require_mapping(choice_properties["selected_security"])
    assert selected_security["title"] == "请选择上市地"

    clarified = hitl.respond(
        owner_id="owner-a",
        interaction_id=choice.interaction_id,
        expected_version=choice.version,
        subject_version=choice.subject_version,
        payload_hash=choice.payload_hash,
        actor_id="owner-a",
        response={"selected_security": "US:NASDAQ:NVDA"},
        resolution="continue",
    )
    scan_review_card = workflow.resume(clarified, runtime)
    assert scan_review_card is not None
    scan_review = repository.list_pending("owner-a")[0]
    assert scan_review.subject_type == "research_scan_review"
    assert scan_review.payload["title"] == "人工确认扫描结论"
    scan_findings = _require_list(scan_review.payload["findings"])
    assert _require_mapping(scan_findings[0])["label"] == "待确认信号"

    reviewed_scan = hitl.respond(
        owner_id="owner-a",
        interaction_id=scan_review.interaction_id,
        expected_version=scan_review.version,
        subject_version=scan_review.subject_version,
        payload_hash=scan_review.payload_hash,
        actor_id="owner-a",
        response={},
        resolution="confirm",
    )
    plan_approval_card = workflow.resume(reviewed_scan, runtime)
    assert plan_approval_card is not None
    plan_approval = repository.list_pending("owner-a")[0]
    assert plan_approval.subject_type == "research_plan_approval"
    assert set(plan_approval.payload) == {"title", "summary", "facts", "text_fallback"}
    created_plan = planning.get_plan(owner_id="owner-a", plan_id=plan_approval.subject_id)
    assert created_plan.source_references[0].source_type == "research_artifact"

    approved_plan = hitl.respond(
        owner_id="owner-a",
        interaction_id=plan_approval.interaction_id,
        expected_version=plan_approval.version,
        subject_version=plan_approval.subject_version,
        payload_hash=plan_approval.payload_hash,
        actor_id="owner-a",
        response={},
        resolution="confirm",
    )
    artifact_card = workflow.resume(approved_plan, runtime)
    assert artifact_card is not None
    assert artifact_card.kind == "artifact.trade_plan"
    reminder_approval = repository.list_pending("owner-a")[0]
    assert reminder_approval.subject_type == "research_reminder_approval"
    assert reminder_approval.payload["title"] == "批准发送桌面提醒"
    reminder_facts = _require_list(reminder_approval.payload["facts"])
    assert _require_mapping(reminder_facts[1])["detail"] == "desktop_push"

    approved_reminder = hitl.respond(
        owner_id="owner-a",
        interaction_id=reminder_approval.interaction_id,
        expected_version=reminder_approval.version,
        subject_version=reminder_approval.subject_version,
        payload_hash=reminder_approval.payload_hash,
        actor_id="owner-a",
        response={},
        resolution="confirm",
    )
    reminder_card = workflow.resume(approved_reminder, runtime)
    assert reminder_card is not None
    assert reminder_card.kind == "artifact.reminder"
    assert backend.activated_channels == ["desktop_push"]
    plan_review = repository.list_pending("owner-a")[0]
    assert plan_review.subject_type == "research_plan_review"
    review_properties = _require_mapping(plan_review.response_schema["properties"])
    feedback_destinations = _require_mapping(review_properties["feedback_destinations"])
    feedback_items = _require_mapping(feedback_destinations["items"])
    destinations = _require_list(feedback_items["enum"])
    assert destinations == ["future_training_data"]
    x_options = _require_list(feedback_destinations["x-options"])
    assert _require_mapping(x_options[0])["label"] == "训练样本"

    completed_review = hitl.respond(
        owner_id="owner-a",
        interaction_id=plan_review.interaction_id,
        expected_version=plan_review.version,
        subject_version=plan_review.subject_version,
        payload_hash=plan_review.payload_hash,
        actor_id="owner-a",
        response={
            "outcome": "false_positive",
            "note": "这次提醒触发后没有形成有效走势。",
            "feedback_destinations": ["future_training_data"],
        },
        resolution="confirm",
    )
    reviewed_card = workflow.resume(completed_review, runtime)
    assert reviewed_card is not None
    assert reviewed_card.kind == "artifact.trade_plan"
    assert len(runtime.saved_resources) == 1
    assert runtime.saved_resources[0].resource_name == "review_records"
