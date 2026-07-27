"""The single composition root shared by every process entry point."""

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
from trade_agent.apps.conversation_runtime import ConversationRunService, ResearchJourneyBackend
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
from trade_agent.core.runtime import AgentManifest, AgentState
from trade_agent.core.testing import FakeLLMClient, FakeToolGateway
from trade_agent.core.tools import (
    DefaultToolGateway,
    ManifestToolPolicy,
    ToolGateway,
    ToolRegistry,
)


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
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
    """Assemble deterministic phase-one dependencies only."""
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
) -> ApplicationContainer:
    """唯一 production composition root, adapters 可由测试替换。"""

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
    conversation_runtime = ConversationRunService(
        graph=graph,
        database=database,
        checkpointer=checkpointer,
        event_store=event_store,
        hitl_service=hitl_service,
        planning=planning,
        research_journey=research_journey,
        tracer=tracer,
    )
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
