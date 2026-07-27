from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class PlanningRepository(CapabilityRepository, Protocol):
    """交易计划版本仓储 port。"""


__all__ = ["PlanningRepository"]
