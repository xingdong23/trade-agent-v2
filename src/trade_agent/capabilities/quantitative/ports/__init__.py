from typing import Protocol

from trade_agent.capabilities.contracts import CapabilityRepository


class QuantitativeRepository(CapabilityRepository, Protocol):
    """量化 artifact 版本仓储 port。"""


__all__ = ["QuantitativeRepository"]
