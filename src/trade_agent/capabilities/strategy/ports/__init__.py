"""Strategy capability 所需的版本化 repository port。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class StrategyRepository(CapabilityRepository, Protocol):
    """策略版本仓储 port。

    Contract:
        - 保存时必须原子校验 owner 与期望版本。
        - 已发布策略版本必须保持不可变并可按 owner 查询。

    Implemented by:
        ``SQLiteAggregateRepository`` 与 ``InMemoryAggregateRepository``。
    """


__all__ = ["StrategyRepository"]
