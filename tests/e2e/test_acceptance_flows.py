from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient
from httpx import Response

from trade_agent.adapters.market_providers import FakeMarketProvider, FakeProviderScenario
from trade_agent.adapters.notifications import InMemoryNotificationAdapter
from trade_agent.adapters.observability import StructuredTracer, TraceEvent
from trade_agent.adapters.sqlite.json_support import payload_hash
from trade_agent.apps.api import create_app
from trade_agent.apps.cli import execute
from trade_agent.apps.container import ApplicationContainer, build_application_container
from trade_agent.apps.conversation_runtime import (
    ResearchJourneyBackend,
    ResearchJourneyResult,
    SecurityCandidate,
)
from trade_agent.capabilities.contracts import CapabilityResult
from trade_agent.capabilities.market_research.application import (
    ResearchAssemblyService,
    SecurityResearchDraft,
    SecurityResolver,
)
from trade_agent.capabilities.market_research.cards import MarketResearchCardPresenter
from trade_agent.capabilities.market_research.contracts import Market, SecurityId
from trade_agent.capabilities.market_research.domain.evidence import EvidenceAssessment
from trade_agent.capabilities.market_research.domain.models import Evidence, FrozenJsonValue
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSectionKind,
)
from trade_agent.capabilities.market_research.ports import (
    ProviderError,
    ProviderRequestContext,
)
from trade_agent.capabilities.planning.application import PlanDraftRequest, PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.contracts import PlanLineage, PlanStatus, ReviewOutcome
from trade_agent.capabilities.quantitative.application.scanning import (
    ScanEvaluator,
    ScanSubmissionValidator,
)
from trade_agent.capabilities.quantitative.application.summary import (
    PersistedScanResultView,
    ScanResultSummaryProjector,
)
from trade_agent.capabilities.quantitative.cards import QuantitativeCardPresenter
from trade_agent.capabilities.quantitative.contracts import (
    ApprovedModelSnapshot,
    BatchInferenceService,
    ComparisonOperator,
    DataFeatureSnapshot,
    EvaluationMetrics,
    EvaluationResult,
    HardRule,
    ModelRegistry,
    ModelRegistryEntry,
    ModelRuntime,
    ModelStatus,
    RankingDefinition,
    ScanConfiguration,
    ScanDisposition,
    ScanEvaluation,
    ScanSecurityInput,
    ScanUniverseSnapshot,
    StrategyVersionSnapshot,
)
from trade_agent.capabilities.reminder.application import ReminderApplication, ReminderWorker
from trade_agent.capabilities.reminder.cards import ReminderCardPresenter
from trade_agent.capabilities.reminder.contracts import (
    DeliveryStatus,
    ReminderObservation,
    ReminderRule,
    ReminderTrigger,
)
from trade_agent.capabilities.reminder.domain import ReminderStatus
from trade_agent.capabilities.reminder.tools import (
    CreateReminderTool,
    GetReminderTool,
    SetReminderStatusTool,
)
from trade_agent.capabilities.watchlist.application import WatchlistService
from trade_agent.capabilities.watchlist.contracts import ImportStatus
from trade_agent.core.config import AppSettings, AuthenticationSettings, DatabaseSettings
from trade_agent.core.hitl import HumanInteraction, InteractionStatus, InteractionType
from trade_agent.core.llm import JsonValue, LLMMessage, LLMRequest, LLMResponse, ModelRoute
from trade_agent.core.presentation import CardEnvelope, HitlCardPresenter
from trade_agent.core.security import Redactor
from trade_agent.core.testing import FakeLLMClient
from trade_agent.core.tools import ToolRequest

NOW = datetime(2026, 7, 27, 9, 30, tzinfo=UTC)


class RecordingRuntime(ModelRuntime):
    def __init__(self) -> None:
        self.security_rows: list[Mapping[str, float]] = []

    def predict_batch(
        self, model_version_id: str, rows: Sequence[Mapping[str, float]]
    ) -> Sequence[Mapping[str, float]]:
        assert model_version_id == "model-approved"
        self.security_rows.extend(rows)
        return tuple({"up_probability": 0.8} for _ in rows)


class FakeReminderRepository:
    def __init__(self) -> None:
        self.rules: dict[tuple[str, str], ReminderRule] = {}
        self.observations: dict[tuple[str, int], ReminderObservation] = {}
        self.triggers: dict[str, ReminderTrigger] = {}
        self.idempotency: dict[str, ReminderRule] = {}

    def save_rule(
        self,
        rule: ReminderRule,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ReminderRule:
        replay = self.idempotency.get(idempotency_key)
        if replay is not None:
            return replay
        current = self.rules.get((rule.owner_id, rule.reminder_id))
        actual_version = 0 if current is None else current.version
        if actual_version != expected_version:
            raise RuntimeError("concurrent write")
        self.rules[(rule.owner_id, rule.reminder_id)] = rule
        self.idempotency[idempotency_key] = rule
        return rule

    def get_rule(self, owner_id: str, reminder_id: str) -> ReminderRule | None:
        return self.rules.get((owner_id, reminder_id))

    def list_active_rules(self) -> tuple[ReminderRule, ...]:
        return tuple(rule for rule in self.rules.values() if rule.status is ReminderStatus.ACTIVE)

    def previous_observation(
        self, reminder_id: str, rule_version: int
    ) -> ReminderObservation | None:
        return self.observations.get((reminder_id, rule_version))

    def save_observation(
        self, reminder_id: str, rule_version: int, observation: ReminderObservation
    ) -> None:
        self.observations[(reminder_id, rule_version)] = observation

    def latest_trigger(self, reminder_id: str, rule_version: int) -> ReminderTrigger | None:
        candidates = [
            trigger
            for trigger in self.triggers.values()
            if trigger.reminder_id == reminder_id and trigger.rule_version == rule_version
        ]
        return max(candidates, key=lambda item: item.observed_at, default=None)

    def record_trigger(self, trigger: ReminderTrigger) -> bool:
        if trigger.trigger_id in self.triggers:
            return False
        self.triggers[trigger.trigger_id] = trigger
        return True

    def update_trigger(self, trigger: ReminderTrigger) -> ReminderTrigger:
        self.triggers[trigger.trigger_id] = trigger
        return trigger


class ConstantObservationProvider:
    def __init__(self, observation: ReminderObservation) -> None:
        self.observation = observation

    async def observe(self, rule: ReminderRule, *, now: datetime) -> ReminderObservation:
        del rule, now
        return self.observation


class CaptureExporter:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def export(self, event: TraceEvent) -> None:
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class AppBundle:
    client: TestClient
    container: ApplicationContainer
    settings: AppSettings
    llm: FakeLLMClient


def _bundle(tmp_path: Path, *, llm_responses: list[LLMResponse] | None = None) -> AppBundle:
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "acceptance.db"),
        authentication=AuthenticationSettings(mode="development", development_user_id="owner-a"),
    )
    llm = FakeLLMClient(llm_responses)
    container = build_application_container(settings, llm_client=llm)
    app = create_app(settings, container)
    return AppBundle(TestClient(app), container, settings, llm)


def _future_deadline(*, milliseconds: int = 0, minutes: int = 10) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes, milliseconds=milliseconds)


def _security(
    symbol: str = "NVDA",
    *,
    exchange: str = "NASDAQ",
    name: str | None = None,
) -> SecurityId:
    return SecurityId(Market.US, exchange, symbol, name or f"{symbol} Corp")


def _evidence(
    evidence_id: str,
    security: SecurityId,
    *,
    evidence_type: str,
    source_reference: str,
    payload: Mapping[str, FrozenJsonValue],
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        security=security,
        evidence_type=evidence_type,
        provider="fake-provider",
        source_reference=source_reference,
        observed_at=NOW,
        published_at=NOW,
        retrieved_at=NOW,
        payload_hash="1" * 64,
        payload=payload,
        freshness="fresh",
    )


def _full_research_draft(security: SecurityId) -> SecurityResearchDraft:
    claims = {
        ResearchSectionKind.PRICE_VOLUME: (
            ResearchClaim("最新报价约 120 美元, 量价仍有支撑", ("e-quote",), "high"),
        ),
        ResearchSectionKind.TECHNICAL_LEVELS: (
            ResearchClaim("价格仍位于关键支撑位上方", ("e-quote",), "medium"),
        ),
        ResearchSectionKind.FUNDAMENTALS: (
            ResearchClaim("最新披露显示需求仍在扩张", ("e-fundamental",), "medium"),
        ),
        ResearchSectionKind.CATALYSTS: (
            ResearchClaim("AI 资本开支仍是近期催化剂", ("e-fundamental",), "medium"),
        ),
        ResearchSectionKind.RISKS: (
            ResearchClaim("财报波动和估值压缩是主要风险", ("e-quote",), "medium"),
        ),
        ResearchSectionKind.ASSUMPTIONS: (
            ResearchClaim("供需改善假设短期内仍成立", ("e-fundamental",), "medium"),
        ),
        ResearchSectionKind.INVALIDATION: (
            ResearchClaim("跌破关键位则研究假设失效", ("e-quote",), "medium"),
        ),
    }
    return SecurityResearchDraft("research-1", "owner-a", security, claims)


def _research_payload() -> CapabilityResult:
    security = _security()
    artifact = ResearchAssemblyService().assemble_security(
        _full_research_draft(security),
        evidence=(
            _evidence(
                "e-quote",
                security,
                evidence_type="quote",
                source_reference="quote:NVDA",
                payload={"price": 120.0},
            ),
            _evidence(
                "e-fundamental",
                security,
                evidence_type="fundamental",
                source_reference="fundamental:NVDA",
                payload={"revenue_growth": 0.25},
            ),
        ),
        assessment=EvidenceAssessment(("e-quote", "e-fundamental"), (), (), ()),
    )
    return CapabilityResult(
        artifact.artifact_id,
        artifact.version,
        {
            "card_type": "research_artifact",
            "title": f"{artifact.security.symbol} 研究",
            "summary": "结构化研究已绑定 citation 与失效条件",
            "sections": [
                {
                    "title": section.kind.value,
                    "content": " ".join(claim.text for claim in section.claims),
                    "kind": (
                        "risk"
                        if section.kind
                        in {ResearchSectionKind.RISKS, ResearchSectionKind.INVALIDATION}
                        else "analysis"
                    ),
                }
                for section in artifact.sections
            ],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "provider": item.provider,
                    "source_reference": item.source_reference,
                    "freshness": item.freshness,
                    "retrieved_at": item.retrieved_at.isoformat(),
                }
                for item in artifact.evidence
            ],
        },
    )


def _lineage(source_id: str = "scan-result-1") -> PlanLineage:
    return PlanLineage(
        source_type="scan_result",
        source_id=source_id,
        source_version=1,
        evidence_ids=("e-quote", "e-fundamental"),
        strategy_id="strategy-1",
        strategy_version=3,
        model_version_id="model-approved",
    )


def _complete_plan_request(*, source_id: str = "scan-result-1") -> PlanDraftRequest:
    return PlanDraftRequest(
        plan_id="plan-1",
        owner_id="owner-a",
        security_id="US:NASDAQ:NVDA",
        direction="研究确认后的回撤买入计划",
        created_at=NOW,
        source_references=(_lineage(source_id),),
        horizon="20 个交易日",
        entry_condition="回踩后重新站上关键位",
        invalidation_condition="收盘跌破失效位",
        target="到达研究目标区间后复核",
        position_notes="按风险预算分批处理",
        risk_notes="财报和模型漂移风险必须复核",
        field_sources={"direction": "用户输入", "target": "research_artifact"},
    )


def _scan_submission(snapshot: ScanUniverseSnapshot) -> tuple[RecordingRuntime, ScanEvaluation]:
    runtime = RecordingRuntime()
    registry = ModelRegistry()
    metrics = EvaluationMetrics(0.8, 0.03, 0.8, 0.2, 0.12, 5.0, 0.95)
    baseline = EvaluationMetrics(0.6, 0.07, 0.6, 0.3, 0.05, 3.0, 0.9)
    registry.register(
        ModelRegistryEntry(
            "model-approved",
            "US",
            "direction",
            "5d",
            ModelStatus.CANDIDATE,
            EvaluationResult("model-approved", metrics, baseline, True, ()),
        )
    )
    registry.approve("model-approved", actor_id="risk-owner")
    inference = BatchInferenceService(registry, runtime, max_missing_ratio=0.1)
    submission = ScanSubmissionValidator().create(
        scan_id="scan-1",
        owner_id="owner-a",
        strategy=StrategyVersionSnapshot(
            "strategy-1:v3",
            "owner-a",
            True,
            "direction",
            "5d",
            ("trend", "quality"),
            (HardRule("positive-trend", "trend", ComparisonOperator.GREATER_THAN, 0.1),),
        ),
        universe=snapshot,
        data_features=DataFeatureSnapshot(
            "data-2026-07-27",
            "features-v1",
            NOW,
            (
                ScanSecurityInput(
                    "US:NASDAQ:NVDA",
                    "US",
                    "NASDAQ",
                    20_000_000,
                    "features:NVDA",
                    {"trend": 0.6, "quality": 0.2},
                    0.0,
                    False,
                    True,
                    ("e-quote",),
                    ("event risk",),
                    (),
                ),
            ),
        ),
        model=ApprovedModelSnapshot("model-approved", "US", "direction", "5d", True),
        ranking=RankingDefinition("ranking-v1", "up_probability", 1.0, {"quality": 0.1}),
        configuration=ScanConfiguration(
            "scan-config-v1",
            ("NASDAQ", "NYSE"),
            1_000_000,
            0.1,
            0.6,
            {"cost_bps": 5},
        ),
        submitted_at=NOW,
    )
    return runtime, ScanEvaluator(inference).evaluate(submission)


class AcceptanceResearchJourney(ResearchJourneyBackend):
    """用真实 capability service 和确定性 fake 外部依赖驱动验收旅程。"""

    def __init__(self, llm: FakeLLMClient) -> None:
        self._llm = llm
        self._reminders = FakeReminderRepository()
        self._reminder_app = ReminderApplication(self._reminders)

    def resolve(self, symbol: str, *, owner_id: str, run_id: str) -> tuple[SecurityCandidate, ...]:
        del owner_id, run_id
        resolution = SecurityResolver(
            (
                _security(symbol, exchange="NASDAQ", name="NVIDIA Corporation"),
                _security(symbol, exchange="NYSE", name="NVIDIA Depositary"),
            )
        ).resolve(symbol)
        return tuple(
            SecurityCandidate(
                f"{item.market.value}:{item.exchange}:{item.symbol}",
                f"{item.display_name} ({item.exchange})",
            )
            for item in resolution.candidates
        )

    def prepare(self, security_id: str, *, owner_id: str, run_id: str) -> ResearchJourneyResult:
        del run_id
        assert owner_id == "owner-a"
        assert security_id == "US:NASDAQ:NVDA"
        research_card = MarketResearchCardPresenter().present(_research_payload())

        watchlist = WatchlistService(owner_id=owner_id, watchlist_id="journey-watchlist")
        imported = watchlist.classify_import((("NVDA", security_id, ImportStatus.ACCEPTED),))
        watchlist.approve_import(
            imported,
            actor_owner_id=owner_id,
            approved=True,
            idempotency_key="journey-watchlist-import",
            source_type="research",
            source_reference=research_card.source.source_id,
            imported_at=NOW,
        )
        universe = watchlist.freeze_universe(actor_owner_id=owner_id, created_at=NOW)
        _, evaluation = _scan_submission(
            ScanUniverseSnapshot(universe.snapshot_id, owner_id, universe.security_ids)
        )
        result = evaluation.results[0]
        presenter = QuantitativeCardPresenter()
        progress_started = presenter.present(
            CapabilityResult(
                "journey-scan",
                1,
                {
                    "card_type": "scan_progress",
                    "status": "running",
                    "completed": 0,
                    "total": 1,
                    "current_step": security_id,
                    "eta_seconds": 1,
                },
            )
        )
        progress_completed = presenter.present(
            CapabilityResult(
                "journey-scan",
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
        scan_card = presenter.present(
            CapabilityResult(
                "journey-scan-result",
                1,
                {
                    "card_type": "scan_result",
                    "security_id": result.security_id,
                    "status": "match",
                    "score": result.score,
                    "probability": result.probability,
                    "matched_conditions": [item.condition_id for item in result.matched_conditions],
                    "exclusions": [],
                    "risks": list(result.risks),
                    "gaps": list(result.gaps),
                    "evidence_ids": list(result.evidence_refs),
                    "model_version_id": result.model_version_id,
                    "feature_snapshot_id": result.feature_snapshot_id,
                },
            )
        )
        return ResearchJourneyResult(
            security_id,
            research_card,
            progress_started,
            progress_completed,
            scan_card,
            {
                "scan_result_id": scan_card.source.source_id,
                "direction": "研究确认后的回撤买入计划",
                "horizon": "20 个交易日",
                "entry_condition": "回踩后重新站上关键位",
                "invalidation_condition": "收盘跌破失效位",
                "target": "到达研究目标区间后复核",
                "position_notes": "按风险预算分批处理",
                "risk_notes": "财报和模型漂移风险必须复核。",
            },
        )

    def summarize(self, scan_result: Mapping[str, JsonValue], *, owner_id: str, run_id: str) -> str:
        projected = ScanResultSummaryProjector.project(
            (PersistedScanResultView(run_id, "journey-scan-result", 1, scan_result),)
        )
        response = asyncio.run(
            self._llm.complete(
                LLMRequest(
                    route=ModelRoute("research_summarizer"),
                    messages=(LLMMessage("user", str(projected)),),
                    response_schema={
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "required": ["summary"],
                    },
                    prompt_version="research-summary.v1",
                    metadata={"owner_id": owner_id, "run_id": run_id},
                )
            )
        )
        summary = response.structured.get("summary") if response.structured else None
        if not isinstance(summary, str) or not summary:
            raise ValueError("研究总结缺少 summary")
        return summary

    def activate_reminder(
        self,
        *,
        owner_id: str,
        plan_id: str,
        interaction_id: str,
        idempotency_key: str,
    ) -> CardEnvelope:
        reminder_id = f"reminder:{plan_id}"
        asyncio.run(
            CreateReminderTool(self._reminder_app).handle(
                ToolRequest(
                    "reminder.create",
                    {
                        "reminder_id": reminder_id,
                        "owner_id": owner_id,
                        "plan_id": plan_id,
                        "rule_type": "scheduled_review",
                        "condition": {"scheduled_at": (NOW + timedelta(days=5)).isoformat()},
                        "notification_channel": "in_app",
                        "cooldown_seconds": 300,
                    },
                    idempotency_key=f"{idempotency_key}:create",
                )
            )
        )
        asyncio.run(
            SetReminderStatusTool(self._reminder_app).handle(
                ToolRequest(
                    "reminder.set_status",
                    {
                        "reminder_id": reminder_id,
                        "owner_id": owner_id,
                        "target_status": "active",
                        "approved": True,
                        "actor_id": owner_id,
                        "payload_hash": "approved-reminder-payload",
                    },
                    idempotency_key=f"{idempotency_key}:activate",
                    approval_interaction_id=interaction_id,
                )
            )
        )
        artifact = asyncio.run(
            GetReminderTool(self._reminder_app).handle(
                ToolRequest("reminder.get", {"reminder_id": reminder_id, "owner_id": owner_id})
            )
        )
        return ReminderCardPresenter().present(
            CapabilityResult(reminder_id, 2, dict(artifact.payload))
        )


def test_single_conversation_run_drives_research_scan_plan_reminder_and_review(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        database=DatabaseSettings(path=tmp_path / "conversation-journey.db"),
        authentication=AuthenticationSettings(mode="development", development_user_id="owner-a"),
    )
    llm = FakeLLMClient(
        [
            LLMResponse(
                content='{"summary":"LLM 只总结持久化扫描结果。"}',
                structured={"summary": "LLM 只总结持久化扫描结果。"},
            )
        ]
    )
    journey = AcceptanceResearchJourney(llm)
    container = build_application_container(settings, llm_client=llm, research_journey=journey)
    client = TestClient(create_app(settings, container))
    headers = {"X-User-ID": "owner-a"}

    started = client.post(
        "/api/conversations/runs",
        headers=headers,
        json={"thread_id": "research-journey", "message": "研究 NVDA 并生成交易计划"},
    )
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "waiting_for_human"
    choice = run["card"]
    assert choice["kind"] == "interaction.choice"
    assert execute(("hitl", "list"), container=container, settings=settings)["pending"]

    def respond(
        card: Mapping[str, JsonValue], action: str, values: Mapping[str, JsonValue]
    ) -> Response:
        source = card["source"]
        assert isinstance(source, Mapping)
        return cast(
            Response,
            client.post(
                f"/api/hitl/{source['source_id']}/responses",
                headers=headers,
                json={
                    "action": action,
                    "values": dict(values),
                    "interaction_version": source["version"],
                    "payload_hash": card["payload_hash"],
                    "idempotency_key": f"journey:{source['source_id']}:{action}",
                    "card_revision": card["revision"],
                },
            ),
        )

    clarified = respond(choice, "continue", {"selected_security": "US:NASDAQ:NVDA"})
    assert clarified.status_code == 200, clarified.text
    scan_review = clarified.json()["card"]
    assert scan_review["kind"] == "interaction.review"

    reviewed_scan = respond(scan_review, "confirm", {})
    assert reviewed_scan.status_code == 200, reviewed_scan.text
    plan_approval = reviewed_scan.json()["card"]
    assert plan_approval["kind"] == "interaction.approval"
    assert llm.requests[0].route.name == "research_summarizer"
    assert "persisted_payload" in llm.requests[0].messages[0].content

    approved_plan = respond(plan_approval, "confirm", {})
    assert approved_plan.status_code == 200, approved_plan.text
    plan_artifact = approved_plan.json()["card"]
    assert plan_artifact["kind"] == "artifact.trade_plan"
    assert "已激活" in plan_artifact["data"]["summary"]

    pending = client.get("/api/hitl/pending?thread_id=research-journey", headers=headers).json()[
        "items"
    ]
    reminder_approval = pending[0]["card"]
    assert reminder_approval["kind"] == "interaction.approval"
    approved_reminder = respond(reminder_approval, "confirm", {})
    assert approved_reminder.status_code == 200, approved_reminder.text
    assert approved_reminder.json()["card"]["kind"] == "artifact.reminder"

    pending = client.get("/api/hitl/pending?thread_id=research-journey", headers=headers).json()[
        "items"
    ]
    final_review = pending[0]["card"]
    assert final_review["kind"] == "interaction.review"
    completed = respond(final_review, "confirm", {})
    assert completed.status_code == 200, completed.text
    assert completed.json()["card"]["kind"] == "artifact.trade_plan"
    assert "已复盘" in completed.json()["card"]["data"]["summary"]

    assert (
        client.get("/api/hitl/pending?thread_id=research-journey", headers=headers).json()["items"]
        == []
    )
    artifact_items = client.get(
        "/api/artifacts?thread_id=research-journey", headers=headers
    ).json()["items"]
    artifact_kinds = {item["card"]["kind"] for item in artifact_items}
    assert {
        "artifact.research",
        "artifact.scan_result",
        "artifact.trade_plan",
        "artifact.reminder",
    } <= artifact_kinds
    reviews = client.get("/api/reviews", headers=headers).json()["items"]
    assert reviews[0]["payload"]["outcome"] == "useful"
    events = client.get(f"/api/runs/{run['run_id']}/events?after=0", headers=headers).text
    assert events.index("artifact.research") < events.index("progress.scan")
    assert events.index("progress.scan") < events.index("artifact.scan_result")
    assert "event: card.resolved" in events


def _approval_interaction(
    interaction_id: str,
    *,
    subject_type: str,
    subject_id: str,
    subject_version: int,
    title: str,
    payload: Mapping[str, JsonValue] | None = None,
    version: int = 1,
    interaction_type: InteractionType = InteractionType.APPROVAL,
    deadline: datetime | None = None,
) -> HumanInteraction:
    body = dict(payload or {})
    body.setdefault("title", title)
    body.setdefault("summary", title)
    body.setdefault("text_fallback", title)
    return HumanInteraction(
        interaction_id=interaction_id,
        owner_id="owner-a",
        interaction_type=interaction_type,
        status=InteractionStatus.PENDING,
        payload=body,
        version=version,
        thread_id="thread-1",
        run_id="run-1",
        subject_type=subject_type,
        subject_id=subject_id,
        subject_version=subject_version,
        payload_hash=payload_hash(body),
        response_schema={
            "type": "object",
            "properties": {"approved": {"type": "boolean", "title": "确认"}},
            "required": ["approved"],
            "additionalProperties": False,
        },
        created_at=NOW,
        deadline=deadline or _future_deadline(),
    )


def test_acceptance_flow_covers_ambiguous_research_scan_plan_reminder_and_review(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path,
        llm_responses=[
            LLMResponse(
                content='{"summary":"LLM 仅输出研究总结, 不参与预测或排序。"}',
                structured={"summary": "LLM 仅输出研究总结, 不参与预测或排序。"},
            )
        ],
    )
    headers = {"X-User-ID": "owner-a"}
    run = bundle.client.post(
        "/api/conversations/runs",
        headers=headers,
        json={"thread_id": "thread-1", "message": "研究 NVDA 并生成交易计划"},
    )
    assert run.status_code == 200
    run_id = run.json()["run_id"]
    events = bundle.client.get(f"/api/runs/{run_id}/events?after=0", headers=headers)
    assert "event: run.started" in events.text

    primary = _security("NVDA", exchange="NASDAQ", name="NVIDIA Corporation")
    duplicate = _security("NVDA", exchange="NYSE", name="NVIDIA Depositary")
    resolver = SecurityResolver((primary, duplicate))
    resolution = resolver.resolve("NVDA")
    assert resolution.status.value == "ambiguous"

    clarification_payload = {
        "title": "请选择具体美股证券",
        "description": "同一代码对应多个美国上市标的, 需要先澄清。",
        "text_fallback": "请选择具体证券。",
    }
    clarification = HumanInteraction(
        interaction_id="interaction-clarify-1",
        owner_id="owner-a",
        interaction_type=InteractionType.CLARIFICATION,
        status=InteractionStatus.PENDING,
        payload=clarification_payload,
        version=1,
        thread_id="thread-1",
        run_id=run_id,
        subject_type="security",
        subject_id="nvda",
        subject_version=1,
        payload_hash=payload_hash(clarification_payload),
        response_schema={
            "type": "object",
            "properties": {
                "selected_security": {
                    "type": "string",
                    "title": "候选证券",
                    "enum": ["US:NASDAQ:NVDA", "US:NYSE:NVDA"],
                }
            },
            "required": ["selected_security"],
            "additionalProperties": False,
        },
        created_at=NOW,
        deadline=_future_deadline(),
    )
    service = bundle.container.hitl_service
    assert service is not None
    service.create(clarification)
    cli_pending = execute(("hitl", "list"), container=bundle.container, settings=bundle.settings)
    assert cli_pending["pending"] == [
        {
            "interaction_id": "interaction-clarify-1",
            "type": "clarification",
            "version": 1,
        }
    ]
    clarification_card = HitlCardPresenter().present(clarification)
    assert clarification_card.kind == "interaction.choice"
    clarified = bundle.client.post(
        "/api/hitl/interaction-clarify-1/responses",
        headers=headers,
        json={
            "action": "continue",
            "values": {"selected_security": "US:NASDAQ:NVDA"},
            "interaction_version": 1,
            "subject_version": 1,
            "payload_hash": clarification.payload_hash,
            "idempotency_key": "clarify-nvda",
            "card_revision": 1,
        },
    )
    assert clarified.status_code == 200
    assert clarified.json()["status"] == "resolved"

    research_card = MarketResearchCardPresenter().present(_research_payload())
    assert research_card.kind == "artifact.research"
    assert "freshness=fresh" in str(research_card.data["provenance"])

    from trade_agent.capabilities.watchlist.application import WatchlistService
    from trade_agent.capabilities.watchlist.contracts import ImportStatus

    watchlist = WatchlistService(owner_id="owner-a", watchlist_id="watchlist-1")
    imported_rows = watchlist.classify_import((("NVDA", "US:NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    approved_rows = watchlist.approve_import(
        imported_rows,
        actor_owner_id="owner-a",
        approved=True,
        idempotency_key="watchlist-import-1",
        source_type="research",
        source_reference="research-1",
        imported_at=NOW,
    )
    assert approved_rows[0].security_id == "US:NASDAQ:NVDA"
    snapshot = watchlist.freeze_universe(actor_owner_id="owner-a", created_at=NOW)
    more_rows = watchlist.classify_import((("MSFT", "US:NASDAQ:MSFT", ImportStatus.ACCEPTED),))
    watchlist.approve_import(
        more_rows,
        actor_owner_id="owner-a",
        approved=True,
        idempotency_key="watchlist-import-2",
        source_type="manual",
        source_reference="chat-2",
        imported_at=NOW + timedelta(minutes=1),
    )
    assert snapshot.security_ids == ("US:NASDAQ:NVDA",)

    runtime, evaluation = _scan_submission(
        ScanUniverseSnapshot(snapshot.snapshot_id, "owner-a", snapshot.security_ids)
    )
    assert len(runtime.security_rows) == 1
    scan_result = evaluation.results[0]
    assert scan_result.disposition is ScanDisposition.MATCHED
    quant_cards = QuantitativeCardPresenter()
    progress_card = quant_cards.present(
        CapabilityResult(
            "scan-1",
            1,
            {
                "card_type": "scan_progress",
                "status": "running",
                "completed": 0,
                "total": 1,
                "current_step": "US:NASDAQ:NVDA",
                "eta_seconds": 10,
            },
        )
    )
    result_card = quant_cards.present(
        CapabilityResult(
            "scan-result-1",
            1,
            {
                "card_type": "scan_result",
                "security_id": scan_result.security_id,
                "status": "match",
                "score": scan_result.score,
                "probability": scan_result.probability,
                "matched_conditions": [
                    item.condition_id for item in scan_result.matched_conditions
                ],
                "exclusions": [],
                "risks": list(scan_result.risks),
                "gaps": list(scan_result.gaps),
                "evidence_ids": list(scan_result.evidence_refs),
                "model_version_id": scan_result.model_version_id,
                "feature_snapshot_id": scan_result.feature_snapshot_id,
            },
        )
    )
    review_card = HitlCardPresenter().present(
        HumanInteraction(
            interaction_id="interaction-review-1",
            owner_id="owner-a",
            interaction_type=InteractionType.REVIEW,
            status=InteractionStatus.PENDING,
            payload={
                "title": "请确认扫描复核结论",
                "description": "先人工复核再进入计划与提醒。",
                "findings": [{"label": "候选", "detail": "NVDA rank=1", "severity": "medium"}],
                "text_fallback": "请确认扫描复核结论。",
            },
            version=1,
            thread_id="thread-1",
            run_id=run_id,
            subject_type="scan",
            subject_id="scan-1",
            subject_version=1,
            payload_hash=payload_hash(
                {
                    "title": "请确认扫描复核结论",
                    "description": "先人工复核再进入计划与提醒。",
                    "findings": [{"label": "候选", "detail": "NVDA rank=1", "severity": "medium"}],
                    "text_fallback": "请确认扫描复核结论。",
                }
            ),
            response_schema={
                "type": "object",
                "properties": {"approved": {"type": "boolean", "title": "确认"}},
                "required": ["approved"],
            },
            created_at=NOW,
            deadline=NOW + timedelta(minutes=10),
        )
    )
    assert progress_card.kind == "progress.scan"
    assert result_card.kind == "artifact.scan_result"
    assert review_card.kind == "interaction.review"

    llm_request = LLMRequest(
        route=ModelRoute("research_summarizer"),
        messages=(LLMMessage("user", "总结 NVDA 研究与扫描结论"),),
        response_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        prompt_version="research-summary.v1",
    )
    summary = asyncio.run(bundle.llm.complete(llm_request))
    assert summary.structured == {"summary": "LLM 仅输出研究总结, 不参与预测或排序。"}

    planning = PlanningService()
    plan = planning.create_draft(_complete_plan_request(), idempotency_key="plan-draft-1")
    plan_approval = _approval_interaction(
        "interaction-plan-approval-1",
        subject_type="plan",
        subject_id=plan.plan_id,
        subject_version=plan.version,
        title="批准激活 NVDA 交易计划",
        payload={
            "title": "批准激活 NVDA 交易计划",
            "summary": "仅激活计划, 不会下单。",
            "text_fallback": "请确认激活 NVDA 交易计划。",
        },
    )
    service.create(plan_approval)
    plan_response = bundle.client.post(
        "/api/hitl/interaction-plan-approval-1/responses",
        headers=headers,
        json={
            "action": "confirm",
            "values": {"approved": True},
            "interaction_version": 1,
            "subject_version": plan.version,
            "payload_hash": plan_approval.payload_hash,
            "idempotency_key": "approve-plan-1",
            "card_revision": 1,
        },
    )
    assert plan_response.status_code == 200
    active_plan = planning.activate(
        owner_id="owner-a",
        plan_id=plan.plan_id,
        expected_version=plan.version,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=plan.approval_payload_hash,
        approval_interaction_id=plan_approval.interaction_id,
        idempotency_key="activate-plan-1",
        occurred_at=NOW + timedelta(minutes=2),
    )
    plan_artifact = PlanningCardPresenter().plan_artifact(active_plan)
    assert plan_artifact.kind == "artifact.trade_plan"

    reminder_repository = FakeReminderRepository()
    reminder_app = ReminderApplication(reminder_repository)
    reminder_create = CreateReminderTool(reminder_app)
    reminder_get = GetReminderTool(reminder_app)
    reminder_transition = SetReminderStatusTool(reminder_app)
    reminder_result = asyncio.run(
        reminder_create.handle(
            ToolRequest(
                "reminder.create",
                {
                    "reminder_id": "reminder-1",
                    "owner_id": "owner-a",
                    "plan_id": plan.plan_id,
                    "rule_type": "price_threshold",
                    "condition": {
                        "security_id": "NASDAQ:NVDA",
                        "threshold": 130.0,
                        "direction": "crosses_above",
                    },
                    "notification_channel": "in_app",
                    "cooldown_seconds": 300,
                },
                idempotency_key="reminder-create-1",
            )
        )
    )
    reminder = reminder_repository.get_rule("owner-a", "reminder-1")
    assert reminder is not None
    reminder_approval = _approval_interaction(
        "interaction-reminder-approval-1",
        subject_type="reminder",
        subject_id=reminder.reminder_id,
        subject_version=reminder.version,
        title="批准启用 NVDA 提醒",
    )
    service.create(reminder_approval)
    reminder_response = bundle.client.post(
        "/api/hitl/interaction-reminder-approval-1/responses",
        headers=headers,
        json={
            "action": "confirm",
            "values": {"approved": True},
            "interaction_version": 1,
            "subject_version": reminder.version,
            "payload_hash": reminder_approval.payload_hash,
            "idempotency_key": "approve-reminder-1",
            "card_revision": 1,
        },
    )
    assert reminder_response.status_code == 200
    reminder_result = asyncio.run(
        reminder_transition.handle(
            ToolRequest(
                "reminder.set_status",
                {
                    "reminder_id": "reminder-1",
                    "owner_id": "owner-a",
                    "target_status": "active",
                    "approved": True,
                    "actor_id": "owner-a",
                    "payload_hash": "approved-hash",
                },
                idempotency_key="reminder-activate-1",
                approval_interaction_id=reminder_approval.interaction_id,
            )
        )
    )
    reminder_artifact = asyncio.run(
        reminder_get.handle(
            ToolRequest(
                "reminder.get",
                {"reminder_id": "reminder-1", "owner_id": "owner-a"},
            )
        )
    )
    reminder_card = ReminderCardPresenter().present(
        CapabilityResult("reminder-1", 2, dict(reminder_artifact.payload))
    )
    assert reminder_card.kind == "artifact.reminder"

    reminder_worker = ReminderWorker(
        reminder_repository,
        ConstantObservationProvider(
            ReminderObservation(
                "reminder-1",
                NOW + timedelta(days=1),
                "quote-2",
                131.0,
            )
        ),
        InMemoryNotificationAdapter(),
    )
    reminder_repository.save_observation(
        "reminder-1",
        2,
        ReminderObservation("reminder-1", NOW + timedelta(days=1, minutes=-1), "quote-1", 129.0),
    )
    delivered = asyncio.run(reminder_worker.run_once(now=NOW + timedelta(days=1)))
    assert delivered[0].delivery_status is DeliveryStatus.DELIVERED

    review = planning.record_review(
        owner_id="owner-a",
        review_id="review-1",
        subject_type="plan",
        subject_id=active_plan.plan_id,
        subject_version=active_plan.version,
        outcome=ReviewOutcome.FALSE_POSITIVE,
        annotations={"note": "价格触发后未延续"},
        lineage=(),
        feedback_destinations=("future_strategy_draft", "future_training_data"),
        actor_id="owner-a",
        approval_interaction_id="interaction-review-1",
        idempotency_key="plan-review-1",
        created_at=NOW + timedelta(days=5),
    )
    assert review.reviewed_plan is not None
    assert review.reviewed_plan.status is PlanStatus.REVIEWED
    assert review.review.lineage[0].model_version_id == "model-approved"
    assert reminder_result.payload["status"] == "active"


def test_acceptance_flow_rejects_cross_user_tampering_timeouts_non_us_and_provider_faults(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    plan_saved = bundle.client.post(
        "/api/plans",
        headers={"X-User-ID": "owner-a"},
        json={"resource_id": "plan-1", "expected_version": 0, "payload": {"status": "draft"}},
    )
    assert plan_saved.status_code == 200
    foreign_plan = bundle.client.get("/api/plans/plan-1", headers={"X-User-ID": "owner-b"})
    assert foreign_plan.status_code == 404

    service = bundle.container.hitl_service
    assert service is not None
    interaction = _approval_interaction(
        "interaction-safety-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=1,
        title="确认计划",
    )
    service.create(interaction)
    foreign = bundle.client.post(
        "/api/hitl/interaction-safety-1/responses",
        headers={"X-User-ID": "owner-b"},
        json={
            "action": "confirm",
            "values": {"approved": True},
            "interaction_version": 1,
            "subject_version": 1,
            "payload_hash": interaction.payload_hash,
            "idempotency_key": "foreign-attempt",
            "card_revision": 1,
        },
    )
    assert foreign.status_code == 404
    tampered = bundle.client.post(
        "/api/hitl/interaction-safety-1/responses",
        headers={"X-User-ID": "owner-a"},
        json={
            "action": "confirm",
            "values": {"approved": True},
            "interaction_version": 1,
            "subject_version": 99,
            "payload_hash": "tampered",
            "idempotency_key": "tampered-attempt",
            "card_revision": 1,
        },
    )
    assert tampered.status_code == 409

    expiring = _approval_interaction(
        "interaction-expiring-1",
        subject_type="plan",
        subject_id="plan-2",
        subject_version=1,
        title="将超时的审批",
        deadline=_future_deadline(milliseconds=5, minutes=0),
    )
    service.create(expiring)
    time.sleep(0.02)
    expired = service.expire_due("owner-a")
    assert expired[0].status.value == "expired"
    expired_response = bundle.client.post(
        "/api/hitl/interaction-expiring-1/responses",
        headers={"X-User-ID": "owner-a"},
        json={
            "action": "confirm",
            "values": {"approved": True},
            "interaction_version": 2,
            "subject_version": 1,
            "payload_hash": expiring.payload_hash,
            "idempotency_key": "expired-attempt",
            "card_revision": 2,
        },
    )
    assert expired_response.status_code == 409

    planning = PlanningService()
    try:
        planning.create_draft(
            PlanDraftRequest(
                plan_id="plan-hk",
                owner_id="owner-a",
                security_id="HK:HKEX:00700",
                direction="想买腾讯",
                created_at=NOW,
                source_references=(_lineage("research-unsupported"),),
            ),
            idempotency_key="hk-plan-1",
        )
    except ValueError as exc:
        assert "unsupported_market" in str(exc)
    else:
        raise AssertionError("expected unsupported_market")

    unsupported = PlanningCardPresenter().unsupported(
        reference_id="request-1",
        unsupported_kind="execute_trade",
        message="首版不能下单, 只能创建美股交易计划。",
    )
    assert unsupported.kind == "notice.unsupported"
    assert unsupported.actions == ("refresh",)

    provider = FakeMarketProvider(
        (_security(),),
        scenario=FakeProviderScenario.TIMEOUT,
    )
    context = ProviderRequestContext("corr-1", NOW, "test")
    try:
        asyncio.run(provider.quote(_security(), context))
    except ProviderError as exc:
        assert exc.retryable is True
        failure = MarketResearchCardPresenter().present(
            CapabilityResult(
                "job-1",
                1,
                {
                    "card_type": "failure",
                    "title": "研究 provider 降级",
                    "message": str(exc),
                    "error_code": exc.code.value,
                    "retryable": exc.retryable,
                },
            )
        )
    else:
        raise AssertionError("expected provider timeout")
    assert failure.kind == "notice.failure"
    assert failure.actions == ("retry", "cancel")

    exporter = CaptureExporter()
    tracer = StructuredTracer(exporter=exporter, redactor=Redactor(("tax_id",)))
    event = tracer.emit(
        correlation_id="corr-1",
        event_type="hitl.security",
        outcome="rejected",
        attributes={
            "authorization": "Bearer abc.def.ghi",
            "tax_id": "sensitive",
            "message": "provider key-secretvalue failed",
        },
    )
    assert event.attributes == {
        "authorization": "[REDACTED]",
        "tax_id": "[REDACTED]",
        "message": "provider [REDACTED] failed",
    }


def test_acceptance_flow_covers_trade_choice_form_errors_edit_supersede_and_idempotent_confirm(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    headers = {"X-User-ID": "owner-a"}
    presenter = PlanningCardPresenter()

    choice = presenter.intent_choice("choice-1")
    options = choice.data["options"]
    assert isinstance(options, list)
    first_option, second_option, third_option = options
    assert isinstance(first_option, dict)
    assert isinstance(second_option, dict)
    assert isinstance(third_option, dict)
    assert first_option["key"] == "create_trade_plan"
    assert second_option["disabled"] is True
    assert third_option["disabled"] is True

    planning = PlanningService()
    created = planning.create_draft(
        PlanDraftRequest(
            plan_id="plan-1",
            owner_id="owner-a",
            security_id="US:NASDAQ:NVDA",
            direction="我要买 NVDA, 但系统不能下单",
            created_at=NOW,
            source_references=(_lineage(),),
            field_sources={"direction": "用户输入"},
        ),
        idempotency_key="draft-1",
    )
    assert created.missing_fields
    unsupported = presenter.unsupported(
        reference_id="buy-nvda",
        unsupported_kind="execute_trade",
        message="不能直接买入 NVDA, 只能补全字段后创建交易计划。",
    )
    assert unsupported.kind == "notice.unsupported"

    form_payload = {
        "title": "补充 NVDA 计划字段",
        "description": "请合并填写周期, 入场, 失效, 目标, 仓位和风险。",
        "text_fallback": "请补充 NVDA 计划字段。",
    }
    form_interaction = HumanInteraction(
        interaction_id="interaction-form-1",
        owner_id="owner-a",
        interaction_type=InteractionType.EXCEPTION_RESOLUTION,
        status=InteractionStatus.PENDING,
        payload=form_payload,
        version=1,
        thread_id="thread-1",
        run_id="run-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=1,
        payload_hash=payload_hash(form_payload),
        response_schema={
            "type": "object",
            "properties": {
                "horizon": {"type": "string", "title": "计划周期"},
                "entry_condition": {"type": "string", "title": "入场条件"},
                "invalidation_condition": {"type": "string", "title": "失效条件"},
                "target": {"type": "string", "title": "目标条件"},
                "position_notes": {"type": "string", "title": "仓位备注"},
                "risk_notes": {"type": "string", "title": "风险说明"},
            },
            "required": [
                "horizon",
                "entry_condition",
                "invalidation_condition",
                "target",
                "position_notes",
                "risk_notes",
            ],
            "additionalProperties": False,
        },
        created_at=NOW,
        deadline=_future_deadline(),
    )
    service = bundle.container.hitl_service
    assert service is not None
    service.create(form_interaction)
    hitl_presenter = HitlCardPresenter()
    form_card = hitl_presenter.present(form_interaction)
    assert form_card.kind == "interaction.form"
    invalid = bundle.client.post(
        "/api/hitl/interaction-form-1/responses",
        headers=headers,
        json={
            "action": "continue",
            "values": {"horizon": "20d"},
            "interaction_version": 1,
            "subject_version": 1,
            "payload_hash": form_interaction.payload_hash,
            "idempotency_key": "form-invalid-1",
            "card_revision": 1,
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["field_errors"]
    pending_after_invalid = bundle.client.get("/api/hitl/pending", headers=headers).json()
    assert pending_after_invalid[0]["interaction_id"] == "interaction-form-1"

    filled = planning.revise_draft(
        _complete_plan_request(),
        expected_version=1,
        idempotency_key="draft-2",
    )
    approval = presenter.plan_approval(filled)
    edited = planning.revise_draft(
        PlanDraftRequest(
            plan_id="plan-1",
            owner_id="owner-a",
            security_id="US:NASDAQ:NVDA",
            direction="我要买 NVDA, 但这里只生成计划",
            created_at=NOW + timedelta(minutes=1),
            source_references=(_lineage(),),
            horizon="20 个交易日",
            entry_condition="回踩后重新站上关键位",
            invalidation_condition="收盘跌破失效位",
            target="先看前高, 再复核",
            position_notes="按预算分批",
            risk_notes="财报和模型失效风险",
            field_sources={"direction": "用户输入"},
        ),
        expected_version=2,
        idempotency_key="draft-3",
    )
    old_card, new_card = presenter.supersede_after_edit(filled, edited)
    assert approval.kind == "interaction.approval"
    assert old_card.state == "superseded"
    assert new_card.state == "pending"

    approval_interaction = _approval_interaction(
        "interaction-approval-1",
        subject_type="plan",
        subject_id="plan-1",
        subject_version=edited.version,
        title="确认激活 NVDA 计划",
        payload={
            "title": "确认激活 NVDA 计划",
            "summary": "仅创建交易计划, 不会触发下单。",
            "text_fallback": "请确认激活 NVDA 计划。",
        },
    )
    service.create(approval_interaction)
    first_card = hitl_presenter.present(approval_interaction)
    confirm_body = {
        "action": "confirm",
        "values": {"approved": True},
        "interaction_version": 1,
        "subject_version": edited.version,
        "payload_hash": approval_interaction.payload_hash,
        "idempotency_key": "approval-confirm-1",
        "card_revision": 1,
    }
    first = bundle.client.post(
        "/api/hitl/interaction-approval-1/responses",
        headers=headers,
        json=confirm_body,
    )
    replay = bundle.client.post(
        "/api/hitl/interaction-approval-1/responses",
        headers=headers,
        json=confirm_body,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    resolved = service.get("owner-a", "interaction-approval-1")
    assert resolved is not None
    resolved_card = hitl_presenter.present(resolved)
    assert resolved_card.card_id == first_card.card_id
    assert resolved_card.revision == first_card.revision + 1

    active = planning.activate(
        owner_id="owner-a",
        plan_id="plan-1",
        expected_version=edited.version,
        actor_id="owner-a",
        approved=True,
        approved_payload_hash=edited.approval_payload_hash,
        approval_interaction_id="interaction-approval-1",
        idempotency_key="activate-1",
        occurred_at=NOW + timedelta(minutes=2),
    )
    artifact = presenter.plan_artifact(active)
    assert artifact.kind == "artifact.trade_plan"
    summary = artifact.data["summary"]
    assert isinstance(summary, str)
    assert "不提供交易执行能力" in summary
