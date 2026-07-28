"""自然语言会话进入 Supervisor Graph 与注册工作流的唯一入口。

阅读本模块时只需跟踪两个公开入口：

``start_run``
    接收新消息，依次完成 run 建立、结构化分类、Graph 路由、Workflow 校验与启动。
``handle_resolved_interaction``
    接收已经通过 HITL 校验的人工响应，找到原 Workflow 并从暂停点继续。

业务步骤、Card 构造和 SQLite 组织方式由注入模块负责。本模块不枚举具体 Workflow
ID、HITL subject type 或自然语言短语。
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import SQLiteThreadCheckpointer
from trade_agent.apps.graph_invoker import GraphInvoker
from trade_agent.apps.workflows.contracts import (
    ConversationRunResult,
    ConversationRuntime,
    WorkflowStartContext,
)
from trade_agent.apps.workflows.registry import WorkflowRegistry
from trade_agent.core.hitl import HitlService, HumanInteraction, InteractionStatus
from trade_agent.core.presentation import CardEnvelope
from trade_agent.core.runtime import (
    AgentState,
    Intent,
    IntentClassifier,
    RouteIntent,
    normalize_route_intent,
)

_UNAVAILABLE_WORKFLOW_KIND = "conversation_workflow_unavailable"


class ConversationRunService:
    """协调一次会话 run 的启动和 HITL 恢复。

    该类只拥有两个业务入口。Graph 负责确认 Agent 路由，``WorkflowRegistry`` 负责
    验证可执行工作流，``DefaultWorkflowRuntime`` 负责持久化、事件和 Card 投影。
    """

    def __init__(
        self,
        *,
        graph: GraphInvoker,
        checkpointer: SQLiteThreadCheckpointer,
        intent_classifier: IntentClassifier,
        workflow_registry: WorkflowRegistry,
        workflow_runtime: ConversationRuntime,
        unregistered_workflow_message: str,
        tracer: StructuredTracer,
    ) -> None:
        self._graph = graph
        self._checkpointer = checkpointer
        self._intent_classifier = intent_classifier
        self._workflow_registry = workflow_registry
        self._workflow_runtime = workflow_runtime
        self._unregistered_workflow_message = unregistered_workflow_message.strip()
        if not self._unregistered_workflow_message:
            raise ValueError("unregistered_workflow_message 不能为空")
        self._tracer = tracer

    @property
    def tracer(self) -> StructuredTracer:
        """返回当前运行时使用的结构化 tracer。"""

        return self._tracer

    @property
    def hitl_service(self) -> HitlService:
        """返回工作流运行时使用的 HITL 实现。"""

        return self._workflow_runtime.hitl_service

    @property
    def registered_workflow_ids(self) -> tuple[str, ...]:
        """返回当前部署允许启动的 Workflow ID。"""

        return self._workflow_registry.workflow_ids

    def start_run(
        self,
        *,
        owner_id: str,
        thread_id: str,
        message: str,
        correlation_id: str,
    ) -> ConversationRunResult:
        """启动会话并推进到结束或首个 HITL 暂停点。

        Args:
            owner_id: 已认证用户标识，也是资源隔离键。
            thread_id: 前端提供的会话线程标识。
            message: 尚未解释业务含义的用户原始消息。
            correlation_id: 贯穿日志和 trace 的请求关联标识。

        Returns:
            当前 run 的状态、首张 Card 和可选待处理 HITL 标识。

        Side Effects:
            创建 run，记录用户消息，执行一次 Supervisor Graph，并可能启动一个 Workflow。
        """

        # 阶段一：先建立 owner-scoped run。后续事件、Card 和 HITL 都必须引用该 run。
        run_id = str(uuid4())
        self._checkpointer.bind_thread(owner_id=owner_id, thread_id=thread_id)
        self._workflow_runtime.start_run(owner_id=owner_id, run_id=run_id, thread_id=thread_id)

        # 阶段二：分类器负责理解自然语言；运行时只读取结构化结果，不判断关键词。
        classification = self._intent_classifier.classify(message=message, owner_id=owner_id)
        intent_id = normalize_route_intent(classification.intent)
        message_id = self._workflow_runtime.record_user_message(
            owner_id=owner_id,
            run_id=run_id,
            thread_id=thread_id,
            message=message,
            intent_id=intent_id,
            workflow_id=classification.workflow_id,
        )

        # 阶段三：Graph 只确认负责处理请求的 Agent，并执行通用路由门禁。
        graph_state = self._graph.invoke(
            AgentState(
                user_id=owner_id,
                thread_id=thread_id,
                run_id=run_id,
                message=message,
                intent=classification.intent,
                workflow_id=classification.workflow_id,
            )
        )
        selected_agent_id = _selected_agent_id(graph_state.get("selected_agent_id"))
        self._tracer.emit(
            correlation_id=correlation_id,
            event_type="conversation.routed",
            outcome="success",
            attributes={
                "run_id": run_id,
                "agent_id": selected_agent_id,
                "workflow_id": classification.workflow_id,
                "reason_code": classification.reason_code,
            },
        )

        # 阶段四：同时匹配 workflow_id 与 Graph 选择的 agent_id，防止绕过 Supervisor。
        workflow = None
        if classification.workflow_id is not None:
            workflow = self._workflow_registry.resolve_start(
                classification.workflow_id,
                selected_agent_id=selected_agent_id,
            )
        if workflow is None:
            return self._unsupported(run_id, thread_id, owner_id, message_id)

        # 阶段五：具体业务从这里交给 Workflow；通用入口不再参与后续业务分支。
        return replace(
            workflow.start(
                context=WorkflowStartContext(owner_id, thread_id, run_id, classification),
                runtime=self._workflow_runtime,
            ),
            user_message_id=message_id,
        )

    def handle_resolved_interaction(self, interaction: HumanInteraction) -> CardEnvelope | None:
        """幂等消费已解决的 HITL，并委托注册工作流恢复。

        Args:
            interaction: 已完成 owner、版本、payload hash 和响应 schema 校验的人工交互。

        Returns:
            Workflow 恢复后产生的下一张 Card；无需推进时返回 ``None``。

        Side Effects:
            可能推进领域状态、发布 Card，并保存可跨进程重放的恢复收据。
        """

        # API 超时重试时优先返回持久化收据，绝不再次执行业务副作用。
        replay = self._workflow_runtime.load_resume_card(
            interaction.owner_id, interaction.interaction_id
        )
        if replay is not None:
            return replay
        if interaction.status is not InteractionStatus.RESOLVED:
            return None

        # subject_type 只用于查询注册表；通用运行时不认识任何具体 subject type。
        workflow = self._workflow_registry.resolve_resume(interaction.subject_type)
        if workflow is None:
            return None
        card = workflow.resume(interaction, self._workflow_runtime)
        if card is not None:
            # 先持久化最终结果，再返回 API，保证进程重启后仍能安全重放。
            self._workflow_runtime.save_resume_card(
                owner_id=interaction.owner_id,
                interaction_id=interaction.interaction_id,
                card=card,
            )
        return card

    def _unsupported(
        self,
        run_id: str,
        thread_id: str,
        owner_id: str,
        message_id: str,
    ) -> ConversationRunResult:
        """发布配置驱动的失败关闭 Card。"""

        notice = self._workflow_runtime.create_unsupported_notice(
            reference_id=run_id,
            unsupported_kind=_UNAVAILABLE_WORKFLOW_KIND,
            message=self._unregistered_workflow_message,
        )
        self._workflow_runtime.publish_card(owner_id, thread_id, run_id, notice, "card.failed")
        return ConversationRunResult(
            run_id,
            thread_id,
            "unsupported",
            card=notice,
            user_message_id=message_id,
        )


def _selected_agent_id(value: object) -> str:
    """把 Graph 返回值收敛为安全的稳定 Agent ID。"""

    route: RouteIntent | None = value if isinstance(value, (str, Intent)) else None
    return normalize_route_intent(route)


__all__ = ["ConversationRunService", "GraphInvoker"]
