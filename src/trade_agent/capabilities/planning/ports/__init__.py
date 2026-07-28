"""Planning capability 所需的版本化 repository port。"""

from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class PlanningRepository(CapabilityRepository, Protocol):
    """交易计划版本仓储 port。

    Contract:
        - 保存时必须原子校验 owner 与期望版本。
        - 已持久化的计划版本不能被覆盖或跨 owner 读取。

    Implemented by:
        ``SQLiteAggregateRepository`` 与 ``InMemoryAggregateRepository``。
    """


__all__ = ["PlanningRepository"]
