"""把一次自然语言会话推进为可恢复的业务流程。

这是当前系统最值得先读的“主线”模块。它只承担应用层编排：用户消息进入后，
运行时识别当前支持的旅程，调用已注册的 journey 插件，并把结果投影为 Card；
遇到需要人类选择、填写或批准的节点时，journey 先创建 HITL 交互并暂停。前端
提交响应后，:meth:`ConversationRunService.handle_resolved_interaction` 依据
``subject_type`` 找到对应 journey，从暂停点继续推进。

注意：当前第一版采用显式状态机保障流程确定性，LangGraph 负责 Agent 路由骨架。
后续可以逐步把路由交给 Supervisor，但 HITL、安全门禁和持久化边界不应交给 LLM。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import (
    SQLiteAggregateRepository,
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteThreadCheckpointer,
)
from trade_agent.apps.journeys.contracts import (
    ConversationJourney,
    ConversationRunResult,
    ConversationRuntimePort,
    JourneyStartContext,
)
from trade_agent.core.events import RunEvent
from trade_agent.core.hitl import DefaultHitlService, HumanInteraction, InteractionStatus
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource
from trade_agent.core.presentation.projection import HitlCardPresenter, stable_card_id
from trade_agent.core.runtime import AgentState, IntentClassification, IntentClassifier


class GraphInvoker(Protocol):
    """会话运行时所需的最小 LangGraph 调用协议。

    Contract:
        - 实现方只接收可写入 checkpoint 的 ``AgentState``。
        - 返回值不得作为领域事实来源；领域事实必须来自 capability repository。

    Implemented by:
        LangGraph 的 ``CompiledStateGraph`` 和测试图替身。
    """

    def invoke(self, input: AgentState) -> Mapping[str, object]: ...


class JourneyRegistry:
    """保存中台当前装配的启动旅程与恢复路由。

    Contract:
        - ``journey_id`` 与 ``subject_type`` 在一个进程内都必须唯一。
        - Registry 只做查找，不解释自然语言、创建业务对象或吞掉 journey 异常。
        - 只接受同时声明启动和恢复协议的完整 ``ConversationJourney`` 插件。
    """

    def __init__(self) -> None:
        self._journeys_by_id: dict[str, ConversationJourney] = {}
        self._journeys_by_subject_type: dict[str, ConversationJourney] = {}

    def register_conversation_journey(self, journey: ConversationJourney) -> None:
        """注册一个同时支持启动与恢复的完整 journey 插件。

        Args:
            journey: 已声明稳定 ``journey_ids`` 与 ``subject_types`` 的插件对象。

        Raises:
            ValueError: 任一启动 ID 或 subject type 冲突。
        """

        for journey_id in journey.journey_ids:
            normalized = journey_id.strip()
            if not normalized:
                raise ValueError("journey_id 不能为空")
            if normalized in self._journeys_by_id:
                raise ValueError(f"journey 已注册: {normalized}")
        for subject_type in journey.subject_types:
            normalized = subject_type.strip()
            if not normalized:
                raise ValueError("subject_type 不能为空")
            if normalized in self._journeys_by_subject_type:
                raise ValueError(f"subject_type 已注册: {normalized}")
        for journey_id in journey.journey_ids:
            self._journeys_by_id[journey_id] = journey
        for subject_type in journey.subject_types:
            self._journeys_by_subject_type[subject_type] = journey

    def get_conversation_journey(self, journey_id: str) -> ConversationJourney | None:
        """按稳定启动 ID 返回完整 journey 插件。"""

        return self._journeys_by_id.get(journey_id)

    def get_resume_journey(self, subject_type: str) -> ConversationJourney | None:
        """按 subject type 返回恢复所需的 journey 插件。"""

        return self._journeys_by_subject_type.get(subject_type)

    def journey_ids(self) -> tuple[str, ...]:
        """返回当前部署已注册的稳定启动 ID。"""

        return tuple(self._journeys_by_id)


class ConversationRunService(ConversationRuntimePort):
    """协调会话、journey 插件、HITL、Card 和事件持久化。

    Contract:
        - 该类只安排步骤，业务规则属于 journey 对应 capability 的 application/domain 层。
        - 自然语言只能通过 ``IntentClassifier`` 进入结构化路由，不能内置关键词分支。
        - 需要人工决定的节点必须先持久化 HITL，再停止推进。

    Side Effects:
        写入 checkpoint、Card、artifact、run context 和事件仓储。
    """

    def __init__(
        self,
        *,
        graph: GraphInvoker,
        database: SQLiteDatabase,
        checkpointer: SQLiteThreadCheckpointer,
        event_store: SQLiteEventStore,
        hitl_service: DefaultHitlService,
        intent_classifier: IntentClassifier,
        tracer: StructuredTracer | None = None,
        journey_registry: JourneyRegistry | None = None,
    ) -> None:
        self._database = database
        self._graph = graph
        self._checkpointer = checkpointer
        self._events = event_store
        self._hitl = hitl_service
        self._intent_classifier = intent_classifier
        self._tracer = tracer or StructuredTracer()
        self._journey_registry = journey_registry or JourneyRegistry()
        self._cards = SQLiteAggregateRepository(database, "cards")
        self._artifacts = SQLiteAggregateRepository(database, "artifacts")
        self._run_contexts = SQLiteAggregateRepository(database, "run_contexts")
        self._handled_interactions: dict[str, CardEnvelope] = {}

    @property
    def tracer(self) -> StructuredTracer:
        """返回当前运行时使用的结构化 tracer。"""

        return self._tracer

    @property
    def hitl_service(self) -> DefaultHitlService:
        """返回 journey 插件复用的 HITL service。"""

        return self._hitl

    def start_run(
        self,
        *,
        owner_id: str,
        thread_id: str,
        message: str,
        correlation_id: str,
    ) -> ConversationRunResult:
        """启动会话并推进到结束或第一个需要人工输入的节点。

        每次调用都会先创建 ``run_id``、绑定用户与 thread、写入 ``run.started``，
        随后再进入当前支持的旅程。所有不能安全识别的请求都会返回 unsupported
        Card，而不会让 LLM 猜测业务动作。
        """

        run_id = str(uuid4())
        self._checkpointer.bind_thread(owner_id=owner_id, thread_id=thread_id)
        self._events.start_run(owner_id=owner_id, run_id=run_id, thread_id=thread_id)
        classification = self._intent_classifier.classify(message=message, owner_id=owner_id)
        self.append_event(
            owner_id,
            run_id,
            "run.started",
            {
                "thread_id": thread_id,
                "intent": classification.intent.value,
                "journey_id": classification.journey_id,
            },
        )
        self._graph.invoke(
            AgentState(
                user_id=owner_id,
                thread_id=thread_id,
                run_id=run_id,
                message=message,
                intent=classification.intent,
            )
        )
        self._tracer.emit(
            correlation_id=correlation_id,
            event_type="conversation.routed",
            outcome="success",
            attributes={
                "run_id": run_id,
                "agent_id": classification.intent.value,
                "journey_id": classification.journey_id,
                "reason_code": classification.reason_code,
            },
        )

        if classification.journey_id is not None:
            journey = self._journey_registry.get_conversation_journey(classification.journey_id)
            if journey is not None:
                return journey.start(
                    JourneyStartContext(owner_id, thread_id, run_id, classification),
                    self,
                )
        notice = self.create_unsupported_notice(
            reference_id=run_id,
            unsupported_kind="conversation_intent",
            message="当前请求没有已注册的业务旅程, 请补充信息或联系管理员配置能力。",
        )
        self.publish_card(owner_id, thread_id, run_id, notice, "card.failed")
        return ConversationRunResult(run_id, thread_id, "unsupported", card=notice)

    def register_conversation_journey(self, journey: ConversationJourney) -> None:
        """注册一个完整的会话旅程插件。"""

        self._journey_registry.register_conversation_journey(journey)

    def registered_journey_ids(self) -> tuple[str, ...]:
        """返回当前部署实际启用的 Journey ID，供诊断与测试读取。"""

        return self._journey_registry.journey_ids()

    def handle_resolved_interaction(self, interaction: HumanInteraction) -> CardEnvelope | None:
        """消费已解决的 HITL，并把恢复逻辑转交给对应 journey 插件。

        同一 interaction 可能因网络重试被重复提交，因此先查询内存中的处理结果。
        持久层还会校验版本和幂等键，二者共同避免一次批准被执行两次。
        """

        replay = self._handled_interactions.get(interaction.interaction_id)
        if replay is not None:
            return replay
        if interaction.status is not InteractionStatus.RESOLVED:
            return None
        journey = self._journey_registry.get_resume_journey(interaction.subject_type)
        if journey is None:
            return None
        card = journey.resume(interaction, self)
        if card is not None:
            self._handled_interactions[interaction.interaction_id] = card
        return card

    def publish_interaction(self, interaction: HumanInteraction, event_type: str) -> CardEnvelope:
        """保存一个 HITL 并投影为交互 Card。"""

        return self.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            _interaction_card(interaction),
            event_type,
        )

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
        """原子化地保存 Card，并追加供 SSE/Web 消费的领域事件。"""

        repository = self._artifacts if artifact else self._cards
        existing = repository.get(owner_id, card.card_id)
        expected_version = 0 if existing is None else existing.version
        repository.save(
            owner_id=owner_id,
            aggregate_id=card.card_id,
            expected_version=expected_version,
            payload={"thread_id": thread_id, "run_id": run_id, "card": card.to_mapping()},
        )
        self.append_event(
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

    def create_unsupported_notice(
        self,
        *,
        reference_id: str,
        unsupported_kind: str,
        message: str,
        source_type: str = "conversation_request",
        revision: int = 1,
    ) -> CardEnvelope:
        """创建一张通用 unsupported 提示卡。"""

        return CardEnvelope(
            protocol_version=CARD_PROTOCOL_VERSION,
            card_id=stable_card_id("unsupported", reference_id),
            kind="notice.unsupported",
            schema_version=1,
            revision=revision,
            source=CardSource(source_type, reference_id, 1),
            state="failed",
            data={
                "title": "当前请求不受支持",
                "message": message,
                "unsupported_kind": unsupported_kind,
                "unsupported_schema_version": 1,
            },
            actions=("refresh",),
            text_fallback=message,
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
        """保存旅程恢复上下文。"""

        self._run_contexts.save(
            owner_id=owner_id,
            aggregate_id=run_id,
            expected_version=expected_version,
            payload={"thread_id": thread_id, **dict(payload)},
        )

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
        """保存一个不需要 Card 投影的结构化资源。"""

        SQLiteAggregateRepository(self._database, resource_name).save(
            owner_id=owner_id,
            aggregate_id=resource_id,
            expected_version=expected_version,
            payload={"thread_id": thread_id, "run_id": run_id, **dict(payload)},
        )

    def require_run_context(self, owner_id: str, run_id: str) -> Mapping[str, JsonValue]:
        """读取一个必须存在的旅程恢复上下文。"""

        context = self._run_contexts.get(owner_id, run_id)
        if context is None:
            raise RuntimeError("journey run context 不存在")
        return context.payload

    def append_event(
        self, owner_id: str, run_id: str, event_type: str, payload: Mapping[str, JsonValue]
    ) -> None:
        """在一个 run 内追加严格递增的事件序号。"""

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

    @staticmethod
    def required_entity(classification: IntentClassification, name: str) -> str:
        """读取旅程所需实体，并在分类协议不完整时 fail closed。

        Args:
            classification: 已通过分类 adapter 本地校验的结构化结果。
            name: 当前旅程要求的实体名称。

        Returns:
            去除首尾空白并转为大写的实体值。

        Raises:
            ValueError: 分类器选择了旅程，却没有返回该旅程要求的实体。
        """

        value = classification.entity(name)
        if value is None or not value.strip():
            raise ValueError(f"journey {classification.journey_id} 缺少实体 {name}")
        return value.strip().upper()


def _interaction_card(interaction: HumanInteraction) -> CardEnvelope:
    """把后端 HITL 聚合投影为前端统一 Card 协议。"""

    projected = HitlCardPresenter().present(interaction)
    return replace(projected, revision=interaction.version)


__all__ = [
    "ConversationJourney",
    "ConversationRunResult",
    "ConversationRunService",
    "GraphInvoker",
    "JourneyRegistry",
    "JourneyStartContext",
]
