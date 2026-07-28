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
    """市场研究版本仓储 port。

    Contract:
        - 保存时必须原子校验 owner 与期望版本。
        - 查询不得暴露其他 owner 的聚合是否存在。

    Implemented by:
        ``SQLiteAggregateRepository`` 与 ``InMemoryAggregateRepository``。
    """


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
