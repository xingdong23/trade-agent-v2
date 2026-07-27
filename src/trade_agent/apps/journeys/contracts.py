"""会话旅程插件的共享协议与上下文模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.hitl import HumanInteraction
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import IntentClassification


@dataclass(frozen=True, slots=True)
class ConversationRunResult:
    """启动或恢复会话后返回给 API/CLI 的最小结果。

    Attributes:
        run_id: 本次执行的稳定标识。
        thread_id: 归属会话线程。
        status: 执行、等待人工、完成或不支持等状态。
        pending_interaction_id: 暂停时等待的 HITL 标识。
        card: 本次推进产生的最后一张 Card。
    """

    run_id: str
    thread_id: str
    status: str
    pending_interaction_id: str | None = None
    card: CardEnvelope | None = None


@dataclass(frozen=True, slots=True)
class JourneyStartContext:
    """启动一个已注册业务旅程所需的应用上下文。

    Attributes:
        owner_id: 已认证用户，是后续所有资源隔离键。
        thread_id: 旅程归属的会话线程。
        run_id: 本次执行标识。
        classification: 已通过意图 adapter 校验的结构化分类结果。
    """

    owner_id: str
    thread_id: str
    run_id: str
    classification: IntentClassification


class ConversationRuntimePort(Protocol):
    """旅程插件可调用的最小运行时门面。

    Contract:
        - 插件只能通过这些方法发布 Card、保存上下文和读取结构化实体。
        - 运行时门面不暴露具体 capability provider，防止插件绕过应用边界。
        - ``require_run_context`` 只返回已经持久化的 JSON 兼容数据。

    Implemented by:
        ``ConversationRunService``。
    """

    def publish_interaction(self, interaction: HumanInteraction, event_type: str) -> CardEnvelope:
        """保存一个 HITL 并投影为交互 Card。

        Args:
            interaction: 已通过业务层构造完成的人工交互聚合。
            event_type: 需要追加到 run 事件流中的 Card 事件类型。

        Returns:
            与 interaction 对应的统一 Card 表示。
        """

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
        """持久化一张 Card，并同步追加 run 事件。

        Args:
            owner_id: 资源所有者。
            thread_id: 归属会话线程。
            run_id: 归属执行。
            card: 要保存的 Card。
            event_type: 事件流中的类型名称。
            artifact: ``True`` 表示长期业务产物，写入 artifact 仓储。

        Returns:
            原样返回已保存的 Card。
        """

    def create_unsupported_notice(
        self,
        *,
        reference_id: str,
        unsupported_kind: str,
        message: str,
        source_type: str = "conversation_request",
        revision: int = 1,
    ) -> CardEnvelope:
        """创建一张通用 unsupported 提示卡。

        Args:
            reference_id: 业务引用标识，用于稳定生成 Card ID。
            unsupported_kind: 版本稳定的问题类型编码。
            message: 面向用户的明确失败说明。
            source_type: CardSource 中的来源类型。
            revision: 当前 Card 修订号。

        Returns:
            尚未持久化的 unsupported Card。
        """

    def save_run_context(
        self,
        *,
        owner_id: str,
        run_id: str,
        thread_id: str,
        payload: Mapping[str, JsonValue],
        expected_version: int = 0,
    ) -> None:
        """保存旅程恢复上下文。

        Args:
            owner_id: 资源所有者。
            run_id: 会话执行标识。
            thread_id: 归属线程。
            payload: 仅包含 JSON 兼容值的恢复上下文。
            expected_version: 乐观并发版本。
        """

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
        """保存一个不需要 Card 投影的结构化资源。

        Args:
            owner_id: 资源所有者。
            resource_name: 资源集合名称，例如 ``reviews``。
            resource_id: 聚合稳定标识。
            thread_id: 归属会话线程。
            run_id: 归属执行标识。
            payload: 仅包含 JSON 兼容值的结构化资源内容。
            expected_version: 乐观并发版本。
        """

    def require_run_context(self, owner_id: str, run_id: str) -> Mapping[str, JsonValue]:
        """读取一个必须存在的旅程恢复上下文。

        Args:
            owner_id: 资源所有者。
            run_id: 会话执行标识。

        Returns:
            已持久化的恢复上下文。

        Raises:
            RuntimeError: 指定 run 没有可恢复上下文。
        """

    def required_entity(self, classification: IntentClassification, name: str) -> str:
        """从结构化分类结果中读取必填实体。

        Args:
            classification: 已通过 schema 校验的分类结果。
            name: 当前旅程需要的实体名称。

        Returns:
            去除首尾空白并转为大写的实体值。

        Raises:
            ValueError: 分类结果缺少当前旅程所需实体。
        """


class ConversationJourney(Protocol):
    """可插拔会话旅程协议。

    Contract:
        - 一个旅程负责自己声明的 ``journey_ids`` 与 ``subject_types``。
        - ``start`` 与 ``resume`` 必须保持确定性，不能依赖未持久化的瞬时状态。
        - 插件不得解析自然语言；所有输入都来自结构化 classification 或 HITL 响应。

    Implemented by:
        ``PlanningConversationJourney``、``ResearchToPlanJourney`` 和测试 fake journey。
    """

    @property
    def journey_ids(self) -> tuple[str, ...]:
        """返回该旅程负责的稳定启动 ID 列表。"""

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回该旅程负责恢复的稳定 subject type 列表。"""

    def start(
        self,
        context: JourneyStartContext,
        runtime: ConversationRuntimePort,
    ) -> ConversationRunResult:
        """启动一个新的会话旅程。

        Args:
            context: 已认证用户、线程与结构化分类上下文。
            runtime: 仅暴露持久化与投影能力的运行时门面。

        Returns:
            启动后当前推进结果。
        """

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: ConversationRuntimePort,
    ) -> CardEnvelope | None:
        """在一个已解决的 HITL 节点继续推进旅程。

        Args:
            interaction: 已通过版本、权限和 schema 校验的交互聚合。
            runtime: 仅暴露持久化与投影能力的运行时门面。

        Returns:
            本次恢复产生的最终 Card；无需处理时返回 ``None``。
        """


__all__ = [
    "ConversationJourney",
    "ConversationRunResult",
    "ConversationRuntimePort",
    "JourneyStartContext",
]
