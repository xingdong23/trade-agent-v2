"""Domain entities remain owned by their capability."""

from trade_agent.capabilities.market_research.domain import Evidence, SecurityId
from trade_agent.capabilities.planning.domain import Review, TradingPlan
from trade_agent.capabilities.quantitative.domain import ModelVersion, Prediction, Scan
from trade_agent.capabilities.reminder.domain import ReminderRule
from trade_agent.capabilities.strategy.domain import StrategyVersion
from trade_agent.capabilities.watchlist.domain import UniverseSnapshot, Watchlist
from trade_agent.core.events import AuditEvent, RunEvent
from trade_agent.core.hitl import HumanInteraction
from trade_agent.core.security import UserContext


def test_domain_types_are_importable_from_their_owner() -> None:
    assert all(
        value is not None
        for value in (
            UserContext,
            HumanInteraction,
            RunEvent,
            AuditEvent,
            SecurityId,
            Evidence,
            ModelVersion,
            Prediction,
            Scan,
            StrategyVersion,
            Watchlist,
            UniverseSnapshot,
            TradingPlan,
            ReminderRule,
            Review,
        )
    )
