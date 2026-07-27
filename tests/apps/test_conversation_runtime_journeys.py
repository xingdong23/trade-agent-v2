"""验证 ConversationRunService 的 journey 插件扩展点。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trade_agent.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteHitlRepository,
    SQLiteThreadCheckpointer,
)
from trade_agent.apps.conversation_runtime import ConversationRunService
from trade_agent.apps.journeys import (
    ConversationJourney,
    ConversationRunResult,
    ConversationRuntimePort,
    JourneyStartContext,
)
from trade_agent.core.hitl import (
    DefaultHitlService,
    HumanInteraction,
    InteractionStatus,
    InteractionType,
)
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import AgentState, Intent, IntentClassification
from trade_agent.core.testing import MappingIntentClassifier


@dataclass(slots=True)
class _FakeGraph:
    """满足 GraphInvoker 协议的测试替身。"""

    invocations: list[AgentState]

    def invoke(self, input: AgentState) -> dict[str, object]:
        self.invocations.append(input)
        return {}


class _EchoJourney(ConversationJourney):
    """用于验证 runtime 插件扩展点的最小 fake journey。"""

    def __init__(self, *, hitl_service: DefaultHitlService) -> None:
        self._hitl = hitl_service

    @property
    def journey_ids(self) -> tuple[str, ...]:
        return ("fake.echo",)

    @property
    def subject_types(self) -> tuple[str, ...]:
        return ("fake.echo.form",)

    def start(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        interaction = self._hitl.create(
            HumanInteraction(
                interaction_id="fake-echo-interaction",
                owner_id=context.owner_id,
                interaction_type=InteractionType.EXCEPTION_RESOLUTION,
                status=InteractionStatus.PENDING,
                payload={
                    "title": "补充课程示例",
                    "description": "输入一段文本，验证 fake journey 可以独立接入 runtime。",
                    "text_fallback": "请输入课程示例文本。",
                },
                version=1,
                thread_id=context.thread_id,
                run_id=context.run_id,
                subject_type="fake.echo.form",
                subject_id=context.run_id,
                subject_version=1,
                payload_hash="fake-echo-payload",
                response_schema={
                    "type": "object",
                    "properties": {"note": {"type": "string", "minLength": 1}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
                created_at=datetime.now(UTC),
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        card = runtime.publish_interaction(interaction, "card.created")
        return ConversationRunResult(
            context.run_id,
            context.thread_id,
            "waiting_for_human",
            interaction.interaction_id,
            card,
        )

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope | None:
        runtime.publish_interaction(interaction, "card.resolved")
        note = str((interaction.response or {}).get("note", ""))
        card = runtime.create_unsupported_notice(
            reference_id=interaction.interaction_id,
            unsupported_kind="fake_echo_complete",
            message=f"fake journey 已处理: {note}",
            source_type="fake_journey",
        )
        return runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            card,
            "card.created",
        )


def _build_runtime(tmp_path: Path) -> tuple[ConversationRunService, DefaultHitlService, _FakeGraph]:
    database = SQLiteDatabase(tmp_path / "journey-runtime.db")
    database.initialize()
    graph = _FakeGraph([])
    hitl_service = DefaultHitlService(SQLiteHitlRepository(database))
    runtime = ConversationRunService(
        graph=graph,
        database=database,
        checkpointer=SQLiteThreadCheckpointer(database),
        event_store=SQLiteEventStore(database),
        hitl_service=hitl_service,
        intent_classifier=MappingIntentClassifier(
            {
                "课程扩展示例": IntentClassification(
                    Intent.PLANNING,
                    "fake.echo",
                    1.0,
                )
            }
        ),
    )
    return runtime, hitl_service, graph


def test_runtime_can_extend_with_fake_journey_without_modifying_runtime(tmp_path: Path) -> None:
    runtime, hitl_service, graph = _build_runtime(tmp_path)
    runtime.register_conversation_journey(_EchoJourney(hitl_service=hitl_service))

    started = runtime.start_run(
        owner_id="owner-a",
        thread_id="thread-a",
        message="课程扩展示例",
        correlation_id="corr-1",
    )

    assert started.status == "waiting_for_human"
    assert started.pending_interaction_id == "fake-echo-interaction"
    assert started.card is not None
    assert started.card.kind == "interaction.form"
    assert len(graph.invocations) == 1

    resolved = hitl_service.respond(
        owner_id="owner-a",
        interaction_id="fake-echo-interaction",
        expected_version=1,
        subject_version=1,
        payload_hash="fake-echo-payload",
        actor_id="owner-a",
        response={"note": "无需修改 runtime"},
        resolution="continue",
    )
    resumed = runtime.handle_resolved_interaction(resolved)

    assert resumed is not None
    assert resumed.kind == "notice.unsupported"
    assert resumed.data["message"] == "fake journey 已处理: 无需修改 runtime"
