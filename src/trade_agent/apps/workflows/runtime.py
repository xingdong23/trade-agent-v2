"""工作流共享的持久化、事件与 Card 投影实现。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import SQLiteAggregateRepository, SQLiteDatabase, SQLiteEventStore
from trade_agent.core.events import RunEvent
from trade_agent.core.hitl import DefaultHitlService, HumanInteraction
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CARD_PROTOCOL_VERSION, CardEnvelope, CardSource
from trade_agent.core.presentation.projection import HitlCardPresenter, stable_card_id

from .contracts import ConversationRuntime

_CARD_RESOURCE = "cards"
_ARTIFACT_RESOURCE = "artifacts"
_RUN_CONTEXT_RESOURCE = "run_contexts"
_RESUME_RECEIPT_RESOURCE = "workflow_resume_receipts"


@dataclass(frozen=True, slots=True)
class UnsupportedNoticeConfig:
    """通用不支持提示卡的部署级展示策略。

    Attributes:
        title: Notice Card 的用户可见标题。
        actions: 当前部署允许用户执行的语义动作。

    Invariants:
        - 标题与动作由组合根显式注入，运行时不决定产品文案。
    """

    title: str
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.title.strip()
            or not self.actions
            or any(not item.strip() for item in self.actions)
        ):
            raise ValueError("unsupported notice 配置不能为空")


class DefaultWorkflowRuntime(ConversationRuntime):
    """为所有会话工作流提供统一的持久化与投影能力。

    该实现集中管理 Card、artifact、恢复上下文、动态资源、run event 和恢复收据。
    工作流只依赖 ``WorkflowRuntime``，不需要知道 SQLite repository 的组织方式。
    """

    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        event_store: SQLiteEventStore,
        hitl_service: DefaultHitlService,
        tracer: StructuredTracer,
        allowed_resource_names: Iterable[str],
        unsupported_notice: UnsupportedNoticeConfig,
    ) -> None:
        self._events = event_store
        self._hitl = hitl_service
        self._tracer = tracer
        self._cards = SQLiteAggregateRepository(database, _CARD_RESOURCE)
        self._artifacts = SQLiteAggregateRepository(database, _ARTIFACT_RESOURCE)
        self._run_contexts = SQLiteAggregateRepository(database, _RUN_CONTEXT_RESOURCE)
        self._resume_receipts = SQLiteAggregateRepository(database, _RESUME_RECEIPT_RESOURCE)
        resources = tuple(dict.fromkeys(name.strip() for name in allowed_resource_names))
        if any(not name for name in resources):
            raise ValueError("workflow resource name 不能为空")
        self._resources = {
            name: SQLiteAggregateRepository(database, name)
            for name in resources
            if name not in {_CARD_RESOURCE, _ARTIFACT_RESOURCE}
        }
        self._unsupported_notice = unsupported_notice

    @property
    def hitl_service(self) -> DefaultHitlService:
        """返回工作流创建和读取 HITL 所使用的统一实现。"""

        return self._hitl

    def start_run(self, *, owner_id: str, run_id: str, thread_id: str) -> None:
        """创建 run 记录并追加首个生命周期事件。"""

        self._events.start_run(owner_id=owner_id, run_id=run_id, thread_id=thread_id)

    def record_user_message(
        self,
        *,
        owner_id: str,
        run_id: str,
        thread_id: str,
        message: str,
        intent_id: str,
        workflow_id: str | None,
    ) -> str:
        """记录路由元数据和用户消息，返回稳定消息 ID。"""

        self.append_event(
            owner_id,
            run_id,
            "run.started",
            {"thread_id": thread_id, "intent": intent_id, "workflow_id": workflow_id},
        )
        message_id = str(uuid4())
        self.append_event(
            owner_id,
            run_id,
            "message.created",
            {
                "thread_id": thread_id,
                "message": {"id": message_id, "role": "user", "content": message},
            },
        )
        return message_id

    def publish_interaction(self, interaction: HumanInteraction, event_type: str) -> CardEnvelope:
        """把 HITL 聚合投影为 Card 并发布。"""

        projected = HitlCardPresenter().present(interaction)
        return self.publish_card(
            interaction.owner_id,
            interaction.thread_id,
            interaction.run_id,
            replace(projected, revision=interaction.version),
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
        """保存 Card 或 artifact，并发布同一修订对应的 run event。"""

        repository = self._artifacts if artifact else self._cards
        existing = repository.get(owner_id, card.card_id)
        repository.save(
            owner_id=owner_id,
            aggregate_id=card.card_id,
            expected_version=0 if existing is None else existing.version,
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
        """按注入策略创建通用 unsupported Notice Card。"""

        return CardEnvelope(
            protocol_version=CARD_PROTOCOL_VERSION,
            card_id=stable_card_id("unsupported", reference_id),
            kind="notice.unsupported",
            schema_version=1,
            revision=revision,
            source=CardSource(source_type, reference_id, 1),
            state="failed",
            data={
                "title": self._unsupported_notice.title,
                "message": message,
                "unsupported_kind": unsupported_kind,
                "unsupported_schema_version": 1,
            },
            actions=self._unsupported_notice.actions,
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
        """保存工作流恢复所需的结构化上下文。"""

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
        """保存组合根显式允许的结构化资源。"""

        repository = self._resources.get(resource_name)
        if repository is None:
            raise ValueError(f"workflow resource 未注册: {resource_name}")
        repository.save(
            owner_id=owner_id,
            aggregate_id=resource_id,
            expected_version=expected_version,
            payload={"thread_id": thread_id, "run_id": run_id, **dict(payload)},
        )

    def require_run_context(self, owner_id: str, run_id: str) -> Mapping[str, JsonValue]:
        """读取必须存在的工作流恢复上下文。"""

        context = self._run_contexts.get(owner_id, run_id)
        if context is None:
            raise RuntimeError("workflow run context 不存在")
        return context.payload

    def load_resume_card(self, owner_id: str, interaction_id: str) -> CardEnvelope | None:
        """读取已持久化的 HITL 恢复结果。"""

        receipt = self._resume_receipts.get(owner_id, interaction_id)
        if receipt is None:
            return None
        card = receipt.payload.get("card")
        if not isinstance(card, Mapping):
            raise RuntimeError("workflow resume receipt 缺少 Card")
        return CardEnvelope.from_mapping(card)

    def save_resume_card(self, *, owner_id: str, interaction_id: str, card: CardEnvelope) -> None:
        """持久化 HITL 恢复结果，供进程重启和重复请求复用。"""

        existing = self._resume_receipts.get(owner_id, interaction_id)
        if existing is not None:
            return
        self._resume_receipts.save(
            owner_id=owner_id,
            aggregate_id=interaction_id,
            expected_version=0,
            payload={"card": card.to_mapping()},
        )

    def append_event(
        self, owner_id: str, run_id: str, event_type: str, payload: Mapping[str, JsonValue]
    ) -> None:
        """在指定 run 中追加下一个严格连续的事件。"""

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


__all__ = ["DefaultWorkflowRuntime", "UnsupportedNoticeConfig"]
