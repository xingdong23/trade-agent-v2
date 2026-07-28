"""Watchlist capability 所需的版本化 repository port。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class WatchlistRepository(CapabilityRepository, Protocol):
    """关注列表版本仓储 port。

    Contract:
        - 保存时必须原子校验 owner 与期望版本。
        - 查询与冻结快照不得读取其他 owner 的成员关系。

    Implemented by:
        ``SQLiteAggregateRepository`` 与 ``InMemoryAggregateRepository``。
    """


__all__ = ["WatchlistRepository"]
