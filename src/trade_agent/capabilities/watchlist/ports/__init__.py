"""Watchlist capability 所需的版本化 repository port。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class WatchlistRepository(CapabilityRepository, Protocol):
    """关注列表版本仓储 port。"""


__all__ = ["WatchlistRepository"]
