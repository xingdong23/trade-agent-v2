"""应用装配入口。

这个模块是整个系统唯一的 composition root（组合根）：API、CLI 和 worker
都从这里取得已经连接好的对象。领域代码只声明需要什么接口，具体使用 SQLite、
LiteLLM 还是测试替身，由本模块在进程启动时决定。

教学阅读顺序建议先看 :func:`build_application_container`，再沿着它创建的
``ConversationRunService`` 阅读一次完整会话流程。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

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
from trade_agent.apps.journeys import (
    ConversationJourney,
    PlanningConversationJourney,
    PlanningJourneyConfig,
    ResearchJourneyBackend,
    ResearchToPlanJourney,
)
from trade_agent.capabilities.market_research.tools import (
    ResearchSecurityTool,
    ResearchThemeTool,
    ResolveSecurityTool,
)
from trade_agent.capabilities.planning.application import PlanningService
from trade_agent.capabilities.planning.tools import (
    CreatePlanDraftTool,
    RecordPlanningReviewTool,
    TransitionPlanTool,
)
from trade_agent.capabilities.quantitative.tools import (
    GetPredictionTool,
    GetQuantitativeSnapshotTool,
    GetScanStatusTool,
    ListScanResultsTool,
    SubmitScanTool,
)
from trade_agent.capabilities.reminder.tools import (
    CreateReminderTool,
    GetReminderTool,
    SetReminderStatusTool,
)
from trade_agent.capabilities.strategy.tools import PublishStrategyTool
from trade_agent.capabilities.watchlist.tools import (
    AcceptClassificationSuggestionTool,
    ApproveWatchlistImportTool,
    FreezeUniverseTool,
    ValidateWatchlistImportTool,
)
from trade_agent.core.config import AppSettings
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
        research_journey: 可选研究旅程后端。
        tracer: 可选结构化追踪器。
        market_provider: 可替换行情 provider。
        capability_tool_ids: 当前版本声明的 Tool ID 集合。
        worker_ids: 当前部署声明的 worker ID 集合。
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
    research_journey: ResearchJourneyBackend | None = None
    tracer: StructuredTracer | None = None
    market_provider: object | None = None
    capability_tool_ids: tuple[str, ...] = ()
    worker_ids: tuple[str, ...] = ()


_TOOL_TYPES = (
    ResolveSecurityTool,
    ResearchSecurityTool,
    ResearchThemeTool,
    GetPredictionTool,
    GetQuantitativeSnapshotTool,
    SubmitScanTool,
    GetScanStatusTool,
    ListScanResultsTool,
    ValidateWatchlistImportTool,
    ApproveWatchlistImportTool,
    AcceptClassificationSuggestionTool,
    FreezeUniverseTool,
    PublishStrategyTool,
    CreatePlanDraftTool,
    TransitionPlanTool,
    RecordPlanningReviewTool,
    CreateReminderTool,
    SetReminderStatusTool,
    GetReminderTool,
)
_CAPABILITY_TOOL_IDS = tuple(tool_type.manifest.tool_id for tool_type in _TOOL_TYPES)


def build_scaffold_container() -> ApplicationContainer:
    """创建不访问数据库和外部服务的最小容器。

    该入口供架构测试和教学演示使用。LLM 与工具调用都使用确定性 fake，因此
    不应该把它当作生产启动方式。
    """
    return ApplicationContainer(
        graph=build_supervisor_graph(),
        agents=BUSINESS_AGENTS,
        llm_client=FakeLLMClient(),
        tool_gateway=FakeToolGateway(),
        capability_tool_ids=_CAPABILITY_TOOL_IDS,
        worker_ids=("scan-worker", "reminder-worker"),
    )


def build_application_container(
    settings: AppSettings,
    *,
    llm_client: LLMClient | None = None,
    tool_gateway: ToolGateway | None = None,
    market_provider: object | None = None,
    research_journey: ResearchJourneyBackend | None = None,
    intent_classifier: IntentClassifier | None = None,
    conversation_journeys: Iterable[ConversationJourney] | None = None,
    planning_journey_config: PlanningJourneyConfig | None = None,
) -> ApplicationContainer:
    """按照配置连接应用运行时，并允许测试替换外部依赖。

    装配顺序刻意从基础设施向业务入口推进：

    1. 初始化 SQLite 及事件、HITL、checkpoint 仓储；
    2. 创建无基础设施依赖的业务 Service；
    3. 把 Tool 注册到受 Agent manifest 限制的 Gateway；
    4. 最后创建会话运行时，并把所有顶层对象放入容器。

    ``llm_client``、``tool_gateway`` 等关键字参数是测试接缝，也是未来接入真实
    provider 的位置。``conversation_journeys`` 允许部署方整体替换业务旅程集合；
    未提供时才装配首版 Planning 及可选 Research-to-plan 默认插件。
    """

    # 基础设施先完成初始化，后续 repository 才能安全创建并访问表结构。
    database = SQLiteDatabase(
        Path(settings.database.path), busy_timeout_ms=settings.database.busy_timeout_ms
    )
    database.initialize()
    graph = build_supervisor_graph()
    event_store = SQLiteEventStore(database)
    hitl_service = DefaultHitlService(SQLiteHitlRepository(database))
    checkpointer = SQLiteThreadCheckpointer(database)
    planning = PlanningService()
    tracer = StructuredTracer()
    # Tool 是 Agent 可调用的受控业务入口；Service 本身不会直接暴露给 Agent。
    planning_tools = (
        CreatePlanDraftTool(planning),
        TransitionPlanTool(planning),
        RecordPlanningReviewTool(planning),
    )
    resolved_gateway = tool_gateway or DefaultToolGateway(
        ToolRegistry(planning_tools),
        ManifestToolPolicy(
            {item.agent_id: frozenset(item.allowed_tool_ids) for item in BUSINESS_AGENTS}
        ),
    )
    # 会话运行时位于最外层，负责把 Graph、HITL、事件和业务能力串成一条流程。
    conversation_runtime = ConversationRunService(
        graph=graph,
        database=database,
        checkpointer=checkpointer,
        event_store=event_store,
        hitl_service=hitl_service,
        intent_classifier=intent_classifier or ClarificationIntentClassifier(),
        tracer=tracer,
    )
    # 默认集合只是当前产品预置；部署方可以注入任意完整 Journey 插件集合。
    resolved_journeys: tuple[ConversationJourney, ...]
    if conversation_journeys is None:
        defaults: list[ConversationJourney] = [
            PlanningConversationJourney(
                planning=planning,
                hitl_service=hitl_service,
                config=planning_journey_config,
            )
        ]
        if research_journey is not None:
            defaults.append(
                ResearchToPlanJourney(
                    backend=research_journey,
                    planning=planning,
                    hitl_service=hitl_service,
                )
            )
        resolved_journeys = tuple(defaults)
    else:
        resolved_journeys = tuple(conversation_journeys)
    for journey in resolved_journeys:
        conversation_runtime.register_conversation_journey(journey)
    return ApplicationContainer(
        graph=graph,
        agents=BUSINESS_AGENTS,
        llm_client=llm_client or FakeLLMClient(),
        tool_gateway=resolved_gateway,
        database=database,
        event_store=event_store,
        command_store=SQLiteCommandStore(database),
        hitl_service=hitl_service,
        checkpointer=checkpointer,
        conversation_runtime=conversation_runtime,
        research_journey=research_journey,
        tracer=tracer,
        market_provider=market_provider or FakeMarketProvider(()),
        capability_tool_ids=_CAPABILITY_TOOL_IDS,
        worker_ids=("scan-worker", "reminder-worker"),
    )


__all__ = ["ApplicationContainer", "build_application_container", "build_scaffold_container"]
