from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class StrategyRepository(CapabilityRepository, Protocol):
    """策略版本仓储 port。"""


__all__ = ["StrategyRepository"]
