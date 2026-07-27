"""Watchlist、membership、分组和冻结 universe models。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue


@dataclass(frozen=True, slots=True)
class Watchlist:
    watchlist_id: str
    owner_id: str
    name: str
    version: int


class ImportStatus(StrEnum):
    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"
    DUPLICATE = "duplicate"
    UNSUPPORTED_MARKET = "unsupported_market"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ImportRow:
    row_number: int
    raw_value: str
    status: ImportStatus
    security_id: str | None = None
    message: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Provenance:
    source_type: str
    source_reference: str
    imported_at: datetime


@dataclass(frozen=True, slots=True)
class Membership:
    security_id: str
    tags: frozenset[str]
    notes: tuple[str, ...]
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True, slots=True)
class WatchlistGroup:
    group_id: str
    name: str
    security_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class ClassificationSuggestion:
    suggestion_id: str
    security_id: str
    proposed_group_id: str
    source_reference: str
    accepted: bool = False
    decided_by: str | None = None
    accepted_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    snapshot_id: str
    owner_id: str
    source_watchlist_id: str
    security_ids: tuple[str, ...]
    created_at: datetime
    source_group_id: str | None = None
