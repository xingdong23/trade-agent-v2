"""Market Research capability 的证券解析与证据组装用例。"""

from trade_agent.capabilities.contracts import CapabilityApplication
from trade_agent.capabilities.market_research.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)

from .research import (
    ConfidenceBand,
    ResearchAssemblyPolicy,
    ResearchAssemblyService,
    ResearchSafetyError,
    ResearchSafetyPolicy,
    ResearchSafetyValidator,
    SecurityResearchDraft,
)
from .security_resolution import (
    SecurityResolutionCopy,
    SecurityResolver,
    security_resolution_copy_from_settings,
)


class MarketResearchApplication(CapabilityApplication):
    """Phase-one public application boundary."""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"market research command 尚未实现: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"market research query 尚未实现: {query.query_id}")


__all__ = [
    "ConfidenceBand",
    "MarketResearchApplication",
    "ResearchAssemblyPolicy",
    "ResearchAssemblyService",
    "ResearchSafetyError",
    "ResearchSafetyPolicy",
    "ResearchSafetyValidator",
    "SecurityResearchDraft",
    "SecurityResolutionCopy",
    "SecurityResolver",
    "security_resolution_copy_from_settings",
]
