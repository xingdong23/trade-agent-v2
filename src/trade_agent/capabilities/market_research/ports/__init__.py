"""Market Research capability 所需的 repository 与 provider ports。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository

from .providers import (
    CorporateActionProvider,
    FundamentalsProvider,
    KlineProvider,
    NewsSearchProvider,
    NotificationProvider,
    ProviderError,
    ProviderErrorCode,
    ProviderObservation,
    ProviderRequestContext,
    QuoteProvider,
    SecurityLookupProvider,
)


class MarketResearchRepository(CapabilityRepository, Protocol):
    """市场研究版本仓储 port。"""


__all__ = [
    "CorporateActionProvider",
    "FundamentalsProvider",
    "KlineProvider",
    "MarketResearchRepository",
    "NewsSearchProvider",
    "NotificationProvider",
    "ProviderError",
    "ProviderErrorCode",
    "ProviderObservation",
    "ProviderRequestContext",
    "QuoteProvider",
    "SecurityLookupProvider",
]
