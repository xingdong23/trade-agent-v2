"""Versioned strategy definitions."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    strategy_id: str
    owner_id: str
    version: int
    name: str
    status: str
    target: str
    horizon: str
    conditions: tuple[Mapping[str, JsonValue], ...]
    ranking_policy: Mapping[str, JsonValue] = field(default_factory=dict)
