"""市场研究 capability 对外公开的模型与协议。"""

from trade_agent.capabilities.contracts import (
    CapabilityCardPresenter,
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)
from trade_agent.capabilities.market_research.domain import (
    Evidence,
    Market,
    ResearchArtifact,
    SecurityId,
    SecurityResolution,
    SecurityResolutionStatus,
)
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSection,
    ResearchSectionKind,
    SecurityResearchArtifact,
    ThemeCandidate,
    ThemeResearchArtifact,
)

__all__ = [
    "CapabilityCardPresenter",
    "CapabilityCommand",
    "CapabilityQuery",
    "CapabilityResult",
    "Evidence",
    "Market",
    "ResearchArtifact",
    "ResearchClaim",
    "ResearchSection",
    "ResearchSectionKind",
    "SecurityId",
    "SecurityResearchArtifact",
    "SecurityResolution",
    "SecurityResolutionStatus",
    "ThemeCandidate",
    "ThemeResearchArtifact",
]
