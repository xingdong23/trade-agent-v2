"""Quantitative model, prediction, and scan lineage."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class ModelVersion:
    model_version_id: str
    algorithm: str
    status: str
    target: str
    horizon: str
    data_snapshot_id: str
    feature_set_version: str
    artifact_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Prediction:
    prediction_id: str
    owner_id: str
    security_id: str
    model_version_id: str
    feature_snapshot_id: str
    target: str
    horizon: str
    as_of: datetime
    output: Mapping[str, JsonValue]
    uncertainty: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Scan:
    scan_id: str
    owner_id: str
    strategy_version_id: str
    universe_snapshot_id: str
    model_version_id: str
    status: str
    version: int
