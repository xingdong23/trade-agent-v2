"""应用装配入口。

这个模块是整个系统唯一的 composition root（组合根）：API、CLI 和 worker
都从这里取得已经连接好的对象。领域代码只声明需要什么接口，具体使用 SQLite、
LiteLLM 还是测试替身，由本模块在进程启动时决定。

``build_application_container`` 是完整运行时的装配入口，调用方不应在其他模块
重复创建基础设施 Adapter。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from trade_agent.adapters.llm.litellm import LiteLLMClient, LiteLLMRouteConfig
from trade_agent.adapters.market_providers import FakeMarketProvider
from trade_agent.adapters.observability import StructuredTracer
from trade_agent.adapters.sqlite import (
    SQLiteCommandStore,
    SQLiteDatabase,
    SQLiteEventStore,
    SQLiteHitlRepository,
    SQLiteThreadCheckpointer,
)
from trade_agent.agents.supervisor import BUSINESS_AGENTS
from trade_agent.agents.supervisor.graph import build_supervisor_graph
from trade_agent.apps.conversation_runtime import ConversationRunService
from trade_agent.apps.graph_invoker import SupervisorGraphInvoker
from trade_agent.apps.workflows import (
    ConversationWorkflow,
    DefaultWorkflowRuntime,
    PlanningConversationWorkflow,
    PlanningWorkflowConfig,
    ResearchToPlanWorkflow,
    ResearchWorkflowBackend,
    UnsupportedNoticeConfig,
    WorkflowRegistry,
    planning_workflow_config_from_settings,
    research_to_plan_workflow_config_from_settings,
)
from trade_agent.capabilities.planning.application import PlanningService
from trade_agent.capabilities.planning.cards import PlanningCardPresenter
from trade_agent.capabilities.planning.tools import (
    CreatePlanDraftTool,
    RecordPlanningReviewTool,
    TransitionPlanTool,
)
from trade_agent.core.config import AppEnvironment, AppSettings
from trade_agent.core.hitl import DefaultHitlService
from trade_agent.core.llm import LLMClient
from trade_agent.core.runtime import (
    AgentManifest,
    AgentState,
    ClarificationIntentClassifier,
    IntentClassifier,
)
from trade_agent.core.testing import FakeLLMClient, FakeToolGateway
from trade_agent.core.tools import (
    DefaultToolGateway,
    ManifestToolPolicy,
    ToolGateway,
    ToolProtocol,
    ToolRegistry,
)


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """保存一个进程运行所需的顶层对象。

    它不是业务对象，也不负责执行逻辑；它只是显式列出已经完成装配的依赖。
    可选字段表示某些轻量测试只需要图或 ToolGateway，无须启动 SQLite。

    Attributes:
        graph: 已编译的顶层 LangGraph。
        agents: 当前部署注册的业务 Agent manifest。
        llm_client: 供应商无关的大模型调用端口。
        tool_gateway: Agent 调用 capability Tool 的受控入口。
        database: 可选 SQLite 连接管理器。
        event_store: 可选 run 事件存储。
        command_store: 可选幂等 command 存储。
        hitl_service: 可选人机交互服务。
        checkpointer: 可选会话 checkpoint 实现。
        conversation_runtime: 可选会话中台运行时。
        research_workflow: 可选研究工作流后端。
        tracer: 可选结构化追踪器。
        market_provider: 可替换行情 provider。
        capability_tool_ids: 当前版本声明的 Tool ID 集合。
        worker_ids: 当前部署声明的 worker ID 集合。
        resource_names: 当前部署通过通用 API 暴露的资源目录。
    """

    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState]
    agents: tuple[AgentManifest, ...]
    llm_client: LLMClient
    tool_gateway: ToolGateway
    database: SQLiteDatabase | None = None
    event_store: SQLiteEventStore | None = None
    command_store: SQLiteCommandStore | None = None
    hitl_service: DefaultHitlService | None = None
    checkpointer: SQLiteThreadCheckpointer | None = None
    conversation_runtime: ConversationRunService | None = None
    research_workflow: ResearchWorkflowBackend | None = None
    tracer: StructuredTracer | None = None
    market_provider: object | None = None
    capability_tool_ids: tuple[str, ...] = ()
    worker_ids: tuple[str, ...] = ()
    resource_names: tuple[str, ...] = ()


def build_scaffold_container(
    *,
    agents: Iterable[AgentManifest] = BUSINESS_AGENTS,
    capability_tools: Iterable[ToolProtocol] = (),
    worker_ids: Iterable[str] = (),
    resource_names: Iterable[str] = (),
) -> ApplicationContainer:
    """创建不访问数据库和外部服务的最小容器。

    该入口供架构测试和轻量本地验证使用。LLM 与工具调用都使用确定性 fake，
    因此不应该把它当作生产启动方式。
    """
    resolved_agents = tuple(agents)
    resolved_tools = tuple(capability_tools)
    return ApplicationContainer(
        graph=build_supervisor_graph(resolved_agents),
        agents=resolved_agents,
        llm_client=FakeLLMClient(),
        tool_gateway=FakeToolGateway(),
        capability_tool_ids=tuple(tool.manifest.tool_id for tool in resolved_tools),
        worker_ids=tuple(worker_ids),
        resource_names=tuple(resource_names),
    )


def build_application_container(
    settings: AppSettings,
    *,
    llm_client: LLMClient | None = None,
    tool_gateway: ToolGateway | None = None,
    market_provider: object | None = None,
    research_workflow: ResearchWorkflowBackend | None = None,
    intent_classifier: IntentClassifier | None = None,
    conversation_workflows: Iterable[ConversationWorkflow] | None = None,
    planning_workflow_config: PlanningWorkflowConfig | None = None,
    agents: Iterable[AgentManifest] | None = None,
    capability_tools: Iterable[ToolProtocol] | None = None,
    worker_ids: Iterable[str] | None = None,
    resource_names: Iterable[str] | None = None,
) -> ApplicationContainer:
    """按照配置连接应用运行时，并允许测试替换外部依赖。

    装配顺序刻意从基础设施向业务入口推进：

    1. 初始化 SQLite 及事件、HITL、checkpoint 仓储；
    2. 创建无基础设施依赖的业务 Service；
    3. 把 Tool 注册到受 Agent manifest 限制的 Gateway；
    4. 最后创建会话运行时，并把所有顶层对象放入容器。

    ``llm_client``、``tool_gateway`` 等关键字参数是测试接缝，也是未来接入真实
    provider 的位置。``conversation_workflows`` 允许部署方整体替换业务工作流集合；
    未提供时才装配首版 Planning 及可选 Research-to-plan 默认插件。
    """

    # 基础设施先完成初始化，后续 repository 才能安全创建并访问表结构。
    database = SQLiteDatabase(
        Path(settings.database.path), busy_timeout_ms=settings.database.busy_timeout_ms
    )
    database.initialize()
    resolved_agents = tuple(agents) if agents is not None else BUSINESS_AGENTS
    graph = build_supervisor_graph(resolved_agents)
    event_store = SQLiteEventStore(database)
    hitl_service = DefaultHitlService(SQLiteHitlRepository(database))
    checkpointer = SQLiteThreadCheckpointer(
        database,
        namespace=settings.checkpoint.namespace,
    )
    planning = PlanningService()
    resolved_planning_workflow_config = planning_workflow_config or (
        planning_workflow_config_from_settings(
            settings.planning_workflow,
            settings.market,
            settings.hitl,
        )
    )
    tracer = StructuredTracer()
    # Tool 是 Agent 可调用的受控业务入口；Service 本身不会直接暴露给 Agent。
    planning_tools: tuple[ToolProtocol, ...] = (
        CreatePlanDraftTool(planning),
        TransitionPlanTool(planning),
        RecordPlanningReviewTool(planning),
    )
    resolved_tools = tuple(capability_tools) if capability_tools is not None else planning_tools
    tool_registry = ToolRegistry(resolved_tools)
    agent_tool_allowlists = {
        item.agent_id: frozenset(item.allowed_tool_ids) for item in resolved_agents
    }
    unknown_policy_agents = set(settings.agent_tool_policy.allowlists) - set(agent_tool_allowlists)
    if unknown_policy_agents:
        unknown = ", ".join(sorted(unknown_policy_agents))
        raise ValueError(f"Agent Tool policy 引用了未注册 Agent: {unknown}")
    agent_tool_allowlists.update(
        {
            agent_id: frozenset(tool_ids)
            for agent_id, tool_ids in settings.agent_tool_policy.allowlists.items()
        }
    )
    resolved_gateway = tool_gateway or DefaultToolGateway(
        tool_registry,
        ManifestToolPolicy(agent_tool_allowlists),
    )
    # 默认集合只是当前部署预置；部署方可以注入任意完整 Workflow 集合。
    resolved_workflows: tuple[ConversationWorkflow, ...]
    if conversation_workflows is None:
        defaults: list[ConversationWorkflow] = [
            PlanningConversationWorkflow(
                planning=planning,
                hitl_service=hitl_service,
                config=resolved_planning_workflow_config,
            )
        ]
        if research_workflow is not None:
            defaults.append(
                ResearchToPlanWorkflow(
                    backend=research_workflow,
                    planning=planning,
                    hitl_service=hitl_service,
                    interaction_ttl_seconds=settings.hitl.pending_ttl_seconds,
                    text_field_max_length=settings.hitl.text_field_max_length,
                    config=research_to_plan_workflow_config_from_settings(
                        settings.research_to_plan_workflow
                    ),
                    presenter=PlanningCardPresenter(
                        resolved_planning_workflow_config.presenter_config
                    ),
                )
            )
        resolved_workflows = tuple(defaults)
    else:
        resolved_workflows = tuple(conversation_workflows)
    resolved_resource_names = (
        tuple(resource_names) if resource_names is not None else settings.api.resource_names
    )
    workflow_registry = WorkflowRegistry(resolved_workflows)
    workflow_runtime = DefaultWorkflowRuntime(
        database=database,
        event_store=event_store,
        hitl_service=hitl_service,
        tracer=tracer,
        allowed_resource_names=resolved_resource_names,
        unsupported_notice=UnsupportedNoticeConfig(
            title=settings.conversation_runtime.unsupported_notice_title,
            actions=settings.conversation_runtime.unsupported_notice_actions,
        ),
    )
    # 会话入口只协调 Graph 路由与 Workflow 生命周期，不承载业务步骤或存储细节。
    conversation_runtime = ConversationRunService(
        graph=SupervisorGraphInvoker(graph),
        checkpointer=checkpointer,
        intent_classifier=intent_classifier or ClarificationIntentClassifier(),
        workflow_registry=workflow_registry,
        workflow_runtime=workflow_runtime,
        unregistered_workflow_message=settings.conversation_runtime.unregistered_workflow_message,
        tracer=tracer,
    )
    resolved_llm = llm_client or _build_llm_client(settings)
    resolved_market_provider = market_provider
    if resolved_market_provider is None:
        if settings.environment is AppEnvironment.PRODUCTION:
            raise ValueError("production 必须显式注入真实 market provider")
        resolved_market_provider = FakeMarketProvider(())
    return ApplicationContainer(
        graph=graph,
        agents=resolved_agents,
        llm_client=resolved_llm,
        tool_gateway=resolved_gateway,
        database=database,
        event_store=event_store,
        command_store=SQLiteCommandStore(database),
        hitl_service=hitl_service,
        checkpointer=checkpointer,
        conversation_runtime=conversation_runtime,
        research_workflow=research_workflow,
        tracer=tracer,
        market_provider=resolved_market_provider,
        capability_tool_ids=tuple(manifest.tool_id for manifest in tool_registry.manifests()),
        worker_ids=tuple(worker_ids) if worker_ids is not None else settings.worker.worker_ids,
        resource_names=resolved_resource_names,
    )


def _build_llm_client(settings: AppSettings) -> LLMClient:
    """根据类型化路由配置选择真实 LiteLLM adapter 或本地 fake。

    Production 已由 ``AppSettings`` 强制要求至少一个路由，因此不会静默落到 fake。
    Development/Test 未配置路由时保留确定性 fake，支持离线开发与测试。
    """

    if not settings.litellm.routes:
        return FakeLLMClient()
    routes = {
        route_name: LiteLLMRouteConfig(
            logical_route=route_name,
            endpoint=route.endpoint,
            timeout_seconds=route.timeout_seconds,
            max_tokens=route.max_tokens,
            allowed_providers=frozenset(route.allowed_providers),
            concurrency_limit=route.concurrency_limit,
            max_attempts=route.max_attempts,
            budget_usd=route.budget_usd,
            fallback_endpoints=route.fallback_endpoints,
        )
        for route_name, route in settings.litellm.routes.items()
    }
    return LiteLLMClient(routes)


__all__ = ["ApplicationContainer", "build_application_container", "build_scaffold_container"]
