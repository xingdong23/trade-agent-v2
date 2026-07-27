from trade_agent.capabilities.market_research.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)

from .research import (
    ResearchAssemblyService,
    ResearchSafetyError,
    ResearchSafetyValidator,
    SecurityResearchDraft,
)
from .security_resolution import SecurityResolver


class MarketResearchApplication:
    """Phase-one public application boundary."""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"market research command 尚未实现: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"market research query 尚未实现: {query.query_id}")


__all__ = [
    "MarketResearchApplication",
    "ResearchAssemblyService",
    "ResearchSafetyError",
    "ResearchSafetyValidator",
    "SecurityResearchDraft",
    "SecurityResolver",
]
