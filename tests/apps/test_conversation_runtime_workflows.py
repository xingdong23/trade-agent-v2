"""验证 ConversationRunService 的 workflow 插件扩展点。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import (
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteHitlRepository,
    SQLiteThreadCheckpointer,
)
from trade_agent.apps.conversation_runtime import ConversationRunService
from trade_agent.apps.graph_invoker import GraphInvoker
from trade_agent.apps.workflows import (
    ConversationRunResult,
    ConversationWorkflow,
    DefaultWorkflowRuntime,
    UnsupportedNoticeConfig,
    WorkflowRegistry,
    WorkflowRuntime,
    WorkflowStartContext,
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
class _FakeGraph(GraphInvoker):
    """满足 GraphInvoker 协议的测试替身。"""

    invocations: list[AgentState]

    def invoke(self, input: AgentState) -> dict[str, object]:
        self.invocations.append(input)
        return {**input, "selected_agent_id": Intent.PLANNING.value}


@dataclass(slots=True)
class _MismatchedGraph(GraphInvoker):
    """返回与分类结果不一致 Agent ID 的 Supervisor fake。"""

    def invoke(self, input: AgentState) -> dict[str, object]:
        """模拟 Graph 将请求路由给另一个 Agent。"""

        return {**input, "selected_agent_id": Intent.RESEARCH.value}


class _EchoWorkflow(ConversationWorkflow):
    """用于验证 runtime 插件扩展点的最小 fake workflow。"""

    def __init__(self, *, hitl_service: DefaultHitlService) -> None:
        self._hitl = hitl_service

    @property
    def agent_id(self) -> str:
        return Intent.PLANNING.value

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        return ("fake.echo",)

    @property
    def subject_types(self) -> tuple[str, ...]:
        return ("fake.echo.form",)

    def start(
        self,
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
    ) -> ConversationRunResult:
        interaction = self._hitl.create(
            HumanInteraction(
                interaction_id="fake-echo-interaction",
                owner_id=context.owner_id,
                interaction_type=InteractionType.EXCEPTION_RESOLUTION,
                status=InteractionStatus.PENDING,
                payload={
                    "title": "补充扩展示例",
                    "description": "输入一段文本，验证 fake workflow 可以独立接入 runtime。",
                    "text_fallback": "请输入扩展示例文本。",
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
        runtime: WorkflowRuntime,
    ) -> CardEnvelope | None:
        runtime.publish_interaction(interaction, "card.resolved")
        note = str((interaction.response or {}).get("note", ""))
        card = runtime.create_unsupported_notice(
            reference_id=interaction.interaction_id,
            unsupported_kind="fake_echo_complete",
            message=f"fake workflow 已处理: {note}",
            source_type="fake_workflow",
        )
        return runtime.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            card,
            "card.created",
        )


def _build_runtime(
    tmp_path: Path,
    *,
    graph_override: GraphInvoker | None = None,
) -> tuple[ConversationRunService, DefaultHitlService, _FakeGraph]:
    database = SQLiteDatabase(tmp_path / "workflow-runtime.db")
    database.initialize()
    graph = _FakeGraph([])
    hitl_service = DefaultHitlService(SQLiteHitlRepository(database))
    tracer = StructuredTracer()
    workflow = _EchoWorkflow(hitl_service=hitl_service)
    runtime = ConversationRunService(
        graph=graph_override or graph,
        checkpointer=SQLiteThreadCheckpointer(database, namespace="test-workflow"),
        intent_classifier=MappingIntentClassifier(
            {
                "自定义扩展示例": IntentClassification(
                    Intent.PLANNING,
                    "fake.echo",
                    1.0,
                    reason_code="test_fixture",
                )
            }
        ),
        workflow_registry=WorkflowRegistry((workflow,)),
        workflow_runtime=DefaultWorkflowRuntime(
            database=database,
            event_store=SQLiteEventStore(database),
            hitl_service=hitl_service,
            tracer=tracer,
            allowed_resource_names=("reviews",),
            unsupported_notice=UnsupportedNoticeConfig(
                title="测试请求不受支持",
                actions=("refresh",),
            ),
        ),
        unregistered_workflow_message="测试环境没有注册对应 Workflow",
        tracer=tracer,
    )
    return runtime, hitl_service, graph


def test_runtime_can_extend_with_fake_workflow_without_modifying_runtime(tmp_path: Path) -> None:
    runtime, hitl_service, graph = _build_runtime(tmp_path)
    started = runtime.start_run(
        owner_id="owner-a",
        thread_id="thread-a",
        message="自定义扩展示例",
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
    assert resumed.data["message"] == "fake workflow 已处理: 无需修改 runtime"


def test_runtime_preserves_case_sensitive_entity_values() -> None:
    classification = IntentClassification(
        "custom",
        "fake.echo",
        1.0,
        reason_code="test_fixture",
        entities=(("note", "  CamelCase/API-Key  "),),
    )

    context = WorkflowStartContext("owner-a", "thread-a", "run-a", classification)
    assert context.require_entity("note") == "CamelCase/API-Key"


def test_runtime_cannot_bypass_supervisor_agent_route(tmp_path: Path) -> None:
    """Graph 选择的 Agent 与 Workflow 声明不一致时必须安全关闭。"""

    runtime, _, _ = _build_runtime(tmp_path, graph_override=_MismatchedGraph())

    result = runtime.start_run(
        owner_id="owner-a",
        thread_id="thread-a",
        message="自定义扩展示例",
        correlation_id="corr-route-mismatch",
    )

    assert result.status == "unsupported"
    assert result.card is not None
    assert result.card.kind == "notice.unsupported"
