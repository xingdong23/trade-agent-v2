"""Market research domain values owned by this capability."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue

type FrozenJsonValue = (
    str
    | int
    | float
    | bool
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
    | None
)


class Market(StrEnum):
    US = "US"


class SecurityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNSUPPORTED_MARKET = "unsupported_market"


@dataclass(frozen=True, slots=True)
class SecurityId:
    market: Market
    exchange: str
    symbol: str
    display_name: str

    def __post_init__(self) -> None:
        if self.market is not Market.US:
            raise ValueError("首版只允许美股证券")
        if not self.exchange or not self.symbol or not self.display_name:
            raise ValueError("规范证券必须包含交易所、symbol 和展示名称")


@dataclass(frozen=True, slots=True)
class SecurityResolution:
    status: SecurityResolutionStatus
    candidates: tuple[SecurityId, ...] = ()
    message: str = ""

    @property
    def security(self) -> SecurityId | None:
        if self.status is SecurityResolutionStatus.RESOLVED and len(self.candidates) == 1:
            return self.candidates[0]
        return None


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    security: SecurityId
    evidence_type: str
    provider: str
    source_reference: str
    observed_at: datetime | None
    published_at: datetime | None
    retrieved_at: datetime
    payload_hash: str
    payload: Mapping[str, FrozenJsonValue]
    freshness: str
    entitlement: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at 必须包含时区")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at 必须包含时区")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at 必须包含时区")


@dataclass(frozen=True, slots=True)
class ResearchArtifact:
    artifact_id: str
    owner_id: str
    security: SecurityId
    version: int
    evidence_ids: tuple[str, ...]
    claims: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
