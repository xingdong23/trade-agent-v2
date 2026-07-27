"""量化扫描提交快照、确定性条件与结构化结果契约。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue


class ComparisonOperator(StrEnum):
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    EQUAL = "eq"
    NOT_EQUAL = "ne"


class ScanDisposition(StrEnum):
    MATCHED = "matched"
    NON_MATCH = "non_match"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HardRule:
    rule_id: str
    feature_name: str
    operator: ComparisonOperator
    expected: float

    def __post_init__(self) -> None:
        if not self.rule_id.strip() or not self.feature_name.strip():
            raise ValueError("hard rule 必须包含 rule_id 与 feature_name")
        if not math.isfinite(self.expected):
            raise ValueError("hard rule expected 必须是有限数值")

    def matches(self, actual: float) -> bool:
        operators = {
            ComparisonOperator.GREATER_THAN: actual > self.expected,
            ComparisonOperator.GREATER_THAN_OR_EQUAL: actual >= self.expected,
            ComparisonOperator.LESS_THAN: actual < self.expected,
            ComparisonOperator.LESS_THAN_OR_EQUAL: actual <= self.expected,
            ComparisonOperator.EQUAL: actual == self.expected,
            ComparisonOperator.NOT_EQUAL: actual != self.expected,
        }
        return operators[self.operator]


@dataclass(frozen=True, slots=True)
class ConditionOutcome:
    condition_id: str
    matched: bool
    actual: JsonValue
    expected: JsonValue
    message: str


@dataclass(frozen=True, slots=True)
class StrategyVersionSnapshot:
    strategy_version_id: str
    owner_id: str
    published: bool
    target: str
    horizon: str
    required_features: tuple[str, ...]
    hard_rules: tuple[HardRule, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_features", tuple(self.required_features))
        object.__setattr__(self, "hard_rules", tuple(self.hard_rules))


@dataclass(frozen=True, slots=True)
class ScanUniverseSnapshot:
    universe_snapshot_id: str
    owner_id: str
    security_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "security_ids", tuple(self.security_ids))


@dataclass(frozen=True, slots=True)
class ApprovedModelSnapshot:
    model_version_id: str
    market: str
    target: str
    horizon: str
    approved: bool


@dataclass(frozen=True, slots=True)
class ScanSecurityInput:
    security_id: str
    market: str
    exchange: str
    average_dollar_volume: float
    feature_snapshot_id: str
    features: Mapping[str, float | None]
    missing_ratio: float
    out_of_distribution: bool
    data_available: bool
    evidence_refs: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.missing_ratio <= 1.0:
            raise ValueError("missing_ratio 必须位于 0 到 1")
        if not math.isfinite(self.average_dollar_volume):
            raise ValueError("average_dollar_volume 必须是有限数值")
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "gaps", tuple(self.gaps))


@dataclass(frozen=True, slots=True)
class DataFeatureSnapshot:
    data_snapshot_id: str
    feature_set_version: str
    as_of: datetime
    securities: tuple[ScanSecurityInput, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("data/feature snapshot as_of 必须包含时区")
        object.__setattr__(self, "securities", tuple(self.securities))


@dataclass(frozen=True, slots=True)
class RankingDefinition:
    version: str
    probability_key: str
    probability_weight: float
    feature_weights: Mapping[str, float]

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.probability_key.strip():
            raise ValueError("ranking 必须包含 version 与 probability_key")
        weights = dict(self.feature_weights)
        if not math.isfinite(self.probability_weight) or any(
            not math.isfinite(value) for value in weights.values()
        ):
            raise ValueError("ranking weight 必须是有限数值")
        object.__setattr__(self, "feature_weights", MappingProxyType(weights))

    def score(self, *, probability: float, features: Mapping[str, float | None]) -> float:
        score = probability * self.probability_weight
        for name, weight in self.feature_weights.items():
            value = features.get(name)
            if value is not None:
                score += value * weight
        return score


@dataclass(frozen=True, slots=True)
class ScanConfiguration:
    version: str
    allowed_exchanges: tuple[str, ...]
    minimum_dollar_volume: float
    maximum_missing_ratio: float
    minimum_probability: float
    parameters: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.allowed_exchanges:
            raise ValueError("scan config 必须包含 version 与交易所范围")
        if self.minimum_dollar_volume < 0 or not math.isfinite(self.minimum_dollar_volume):
            raise ValueError("minimum_dollar_volume 必须是非负有限数值")
        if not 0.0 <= self.maximum_missing_ratio <= 1.0:
            raise ValueError("maximum_missing_ratio 必须位于 0 到 1")
        if not 0.0 <= self.minimum_probability <= 1.0:
            raise ValueError("minimum_probability 必须位于 0 到 1")
        object.__setattr__(self, "allowed_exchanges", tuple(self.allowed_exchanges))
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(deepcopy(dict(self.parameters))),
        )


@dataclass(frozen=True, slots=True)
class ScanSubmission:
    scan_id: str
    owner_id: str
    strategy: StrategyVersionSnapshot
    universe: ScanUniverseSnapshot
    data_features: DataFeatureSnapshot
    model: ApprovedModelSnapshot
    ranking: RankingDefinition
    configuration: ScanConfiguration
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class ScanResult:
    scan_id: str
    security_id: str
    disposition: ScanDisposition
    rank: int | None
    probability: float | None
    score: float | None
    matched_conditions: tuple[ConditionOutcome, ...]
    excluded_conditions: tuple[ConditionOutcome, ...]
    evidence_refs: tuple[str, ...]
    data_snapshot_id: str
    feature_snapshot_id: str
    feature_set_version: str
    model_version_id: str | None
    ranking_version: str
    risks: tuple[str, ...]
    gaps: tuple[str, ...]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScanEvaluation:
    scan_id: str
    results: tuple[ScanResult, ...]

    @property
    def matched(self) -> tuple[ScanResult, ...]:
        return tuple(item for item in self.results if item.disposition is ScanDisposition.MATCHED)
