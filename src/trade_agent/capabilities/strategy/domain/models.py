"""Versioned strategy definitions."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    """表示一个已版本化的策略定义快照。

    Attributes:
        strategy_id: 策略稳定标识。
        owner_id: 策略所属用户。
        version: 当前版本号。
        name: 策略展示名称。
        status: 当前状态，例如 draft 或 published。
        target: 策略目标变量。
        horizon: 策略适用周期。
        conditions: 结构化入场或筛选条件集合。
        ranking_policy: 对候选证券进行排序的策略配置。
    """

    strategy_id: str
    owner_id: str
    version: int
    name: str
    status: str
    target: str
    horizon: str
    conditions: tuple[Mapping[str, JsonValue], ...]
    ranking_policy: Mapping[str, JsonValue] = field(default_factory=dict)
