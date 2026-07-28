"""核心 Interface 与正式 Implementation 必须保留显式继承关系。"""

import pytest

from trade_agent.adapters.llm.litellm import LiteLLMClient
from trade_agent.adapters.market_providers import FakeMarketProvider
from trade_agent.adapters.memory import InMemoryAggregateRepository
from trade_agent.adapters.sqlite import (
    SQLiteAggregateRepository,
    SQLiteCommandStore,
    SQLiteHitlRepository,
)
from trade_agent.apps.graph_invoker import GraphInvoker, SupervisorGraphInvoker
from trade_agent.apps.workflows import ConversationRuntime, DefaultWorkflowRuntime
from trade_agent.capabilities.contracts import (
    CapabilityApplication,
    CapabilityCardPresenter,
    CapabilityRepository,
)
from trade_agent.capabilities.market_research.application import MarketResearchApplication
from trade_agent.capabilities.market_research.cards import MarketResearchCardPresenter
from trade_agent.capabilities.market_research.ports import (
    CorporateActionProvider,
    FundamentalsProvider,
    KlineProvider,
    NewsSearchProvider,
    NotificationProvider,
    QuoteProvider,
    SecurityLookupProvider,
)
from trade_agent.capabilities.planning.application import PlanningApplication
from trade_agent.capabilities.quantitative.application import QuantitativeApplication
from trade_agent.capabilities.quantitative.application.training import (
    DeterministicRuleBenchmark,
    ProbabilityBenchmark,
    StatisticalBenchmark,
)
from trade_agent.capabilities.quantitative.cards import QuantitativeCardPresenter
from trade_agent.capabilities.reminder.application import ReminderApplication
from trade_agent.capabilities.reminder.cards import ReminderCardPresenter
from trade_agent.capabilities.strategy.application import StrategyApplication
from trade_agent.capabilities.watchlist.application import WatchlistApplication
from trade_agent.capabilities.watchlist.cards import WatchlistCardPresenter
from trade_agent.core.hitl import HitlRepository, HitlService
from trade_agent.core.hitl.service import DefaultHitlService
from trade_agent.core.llm import LLMClient
from trade_agent.core.runtime.execution import CommandStore
from trade_agent.core.runtime.intent import ClarificationIntentClassifier, IntentClassifier
from trade_agent.core.security import AccessPolicy, OwnerAccessPolicy
from trade_agent.core.testing import FakeLLMClient, FakeToolGateway, MappingIntentClassifier
from trade_agent.core.tools import DefaultToolGateway, ToolGateway
from trade_agent.core.tools.policy import ManifestToolPolicy, ToolPolicy


@pytest.mark.parametrize(
    ("interface", "implementation"),
    (
        (IntentClassifier, ClarificationIntentClassifier),
        (IntentClassifier, MappingIntentClassifier),
        (LLMClient, LiteLLMClient),
        (LLMClient, FakeLLMClient),
        (ToolGateway, DefaultToolGateway),
        (ToolGateway, FakeToolGateway),
        (ToolPolicy, ManifestToolPolicy),
        (HitlRepository, SQLiteHitlRepository),
        (HitlService, DefaultHitlService),
        (CommandStore, SQLiteCommandStore),
        (GraphInvoker, SupervisorGraphInvoker),
        (ConversationRuntime, DefaultWorkflowRuntime),
        (CapabilityApplication, MarketResearchApplication),
        (CapabilityApplication, PlanningApplication),
        (CapabilityApplication, QuantitativeApplication),
        (CapabilityApplication, ReminderApplication),
        (CapabilityApplication, StrategyApplication),
        (CapabilityApplication, WatchlistApplication),
        (CapabilityRepository, SQLiteAggregateRepository),
        (CapabilityRepository, InMemoryAggregateRepository),
        (ProbabilityBenchmark, DeterministicRuleBenchmark),
        (ProbabilityBenchmark, StatisticalBenchmark),
        (AccessPolicy, OwnerAccessPolicy),
        (CapabilityCardPresenter, MarketResearchCardPresenter),
        (CapabilityCardPresenter, QuantitativeCardPresenter),
        (CapabilityCardPresenter, ReminderCardPresenter),
        (CapabilityCardPresenter, WatchlistCardPresenter),
        (SecurityLookupProvider, FakeMarketProvider),
        (QuoteProvider, FakeMarketProvider),
        (KlineProvider, FakeMarketProvider),
        (CorporateActionProvider, FakeMarketProvider),
        (FundamentalsProvider, FakeMarketProvider),
        (NewsSearchProvider, FakeMarketProvider),
        (NotificationProvider, FakeMarketProvider),
    ),
)
def test_implementation_explicitly_inherits_interface(
    interface: type[object], implementation: type[object]
) -> None:
    """保证 IDE 可以从 Interface 查找到正式 Implementation。"""

    assert interface in implementation.__mro__
