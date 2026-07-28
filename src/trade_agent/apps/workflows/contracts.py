"""会话工作流的共享协议与上下文模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from trade_agent.core.hitl import HitlService, HumanInteraction
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
        user_message_id: 本次 run 已持久化用户消息的稳定标识。
    """

    run_id: str
    thread_id: str
    status: str
    pending_interaction_id: str | None = None
    card: CardEnvelope | None = None
    user_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowStartContext:
    """启动一个已注册业务工作流所需的应用上下文。

    Attributes:
        owner_id: 已认证用户，是后续所有资源隔离键。
        thread_id: 工作流归属的会话线程。
        run_id: 本次执行标识。
        classification: 已通过意图 adapter 校验的结构化分类结果。
    """

    owner_id: str
    thread_id: str
    run_id: str
    classification: IntentClassification

    def require_entity(self, name: str) -> str:
        """读取工作流声明为必填的结构化实体。

        Args:
            name: 分类协议中的实体名称。

        Returns:
            去除首尾空白后的实体原值。

        Raises:
            ValueError: 分类结果没有提供指定实体。
        """

        value = self.classification.entity(name)
        if value is None or not value.strip():
            raise ValueError(f"workflow {self.classification.workflow_id} 缺少实体 {name}")
        return value.strip()


class WorkflowRuntime(Protocol):
    """工作流可调用的持久化与投影协议。

    Contract:
        - 插件只能通过这些方法发布 Card、保存上下文和读取结构化实体。
        - 运行时门面不暴露具体 capability provider，防止插件绕过应用边界。
        - ``require_run_context`` 只返回已经持久化的 JSON 兼容数据。

    Implemented by:
        ``DefaultWorkflowRuntime``。
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
        """保存工作流恢复上下文。

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
        """读取一个必须存在的工作流恢复上下文。

        Args:
            owner_id: 资源所有者。
            run_id: 会话执行标识。

        Returns:
            已持久化的恢复上下文。

        Raises:
            RuntimeError: 指定 run 没有可恢复上下文。
        """


class ConversationRuntime(WorkflowRuntime, Protocol):
    """会话入口在 Workflow 能力之外需要的运行时协议。

    Contract:
        - run、消息、恢复收据、Card 与事件必须使用同一 owner/run 隔离范围。
        - 恢复收据必须持久化，使进程重启和重复响应返回同一结果。
        - 实现不得根据自然语言、Workflow ID 或 HITL subject type 决定业务分支。

    Implemented by:
        ``DefaultWorkflowRuntime`` 与会话入口集成测试中的显式 fake。
    """

    @property
    def hitl_service(self) -> HitlService:
        """返回当前会话运行时共享的 HITL 服务。"""

    def start_run(self, *, owner_id: str, run_id: str, thread_id: str) -> None:
        """创建一次 owner-scoped run，并写入首个生命周期事件。"""

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
        """保存原始用户消息与结构化路由元数据，并返回消息 ID。"""

    def load_resume_card(self, owner_id: str, interaction_id: str) -> CardEnvelope | None:
        """读取已持久化的 HITL 恢复结果；首次恢复时返回 ``None``。"""

    def save_resume_card(self, *, owner_id: str, interaction_id: str, card: CardEnvelope) -> None:
        """幂等保存 HITL 恢复结果，供重放和进程重启复用。"""


class ConversationWorkflow(Protocol):
    """可注册的会话工作流协议。

    Contract:
        - 一个工作流负责自己声明的 ``workflow_ids`` 与 ``subject_types``。
        - ``agent_id`` 必须对应 Supervisor Graph 已注册的 Agent。
        - ``start`` 与 ``resume`` 必须保持确定性，不能依赖未持久化的瞬时状态。
        - 工作流不得解析自然语言；所有输入都来自结构化 classification 或 HITL 响应。

    Implemented by:
        ``PlanningConversationWorkflow``、``ResearchToPlanWorkflow`` 和测试 fake workflow。
    """

    @property
    def agent_id(self) -> str:
        """返回负责执行该工作流的稳定 Agent ID。"""

    @property
    def workflow_ids(self) -> tuple[str, ...]:
        """返回该工作流负责的稳定启动 ID 列表。"""

    @property
    def subject_types(self) -> tuple[str, ...]:
        """返回该工作流负责恢复的稳定 subject type 列表。"""

    def start(
        self,
        context: WorkflowStartContext,
        runtime: WorkflowRuntime,
    ) -> ConversationRunResult:
        """启动一个新的会话工作流。

        Args:
            context: 已认证用户、线程与结构化分类上下文。
            runtime: 仅暴露持久化与投影能力的运行时门面。

        Returns:
            启动后当前推进结果。
        """

    def resume(
        self,
        interaction: HumanInteraction,
        runtime: WorkflowRuntime,
    ) -> CardEnvelope | None:
        """在一个已解决的 HITL 节点继续推进工作流。

        Args:
            interaction: 已通过版本、权限和 schema 校验的交互聚合。
            runtime: 仅暴露持久化与投影能力的运行时门面。

        Returns:
            本次恢复产生的最终 Card；无需处理时返回 ``None``。
        """


__all__ = [
    "ConversationRunResult",
    "ConversationRuntime",
    "ConversationWorkflow",
    "WorkflowRuntime",
    "WorkflowStartContext",
]
