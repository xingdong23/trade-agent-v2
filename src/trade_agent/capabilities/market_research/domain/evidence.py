"""不可变 evidence 创建、时效、冲突与引用校验。"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from trade_agent.core.llm.contracts import JsonValue

from .models import Evidence, FrozenJsonValue, SecurityId


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    evidence_id: str
    security: SecurityId
    evidence_type: str
    provider: str
    source_reference: str
    observed_at: datetime | None
    published_at: datetime | None
    retrieved_at: datetime
    payload: Mapping[str, JsonValue]
    entitlement: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    evidence_type: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    accepted_evidence_ids: tuple[str, ...]
    rejected_evidence_ids: tuple[str, ...]
    conflicts: tuple[EvidenceConflict, ...]
    gaps: tuple[str, ...]


def _freeze_json(value: JsonValue) -> FrozenJsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def _freeze_object(value: Mapping[str, JsonValue]) -> Mapping[str, FrozenJsonValue]:
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


class EvidenceFactory:
    def __init__(self, freshness_thresholds: Mapping[str, timedelta]) -> None:
        self._thresholds = dict(freshness_thresholds)

    def create(self, source: EvidenceInput) -> Evidence:
        normalized_payload = json.loads(
            json.dumps(source.payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        normalized_entitlement = json.loads(
            json.dumps(
                source.entitlement,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        if not isinstance(normalized_payload, dict) or not isinstance(normalized_entitlement, dict):
            raise ValueError("evidence payload 与 entitlement 必须是 JSON object")
        digest = hashlib.sha256(
            json.dumps(
                normalized_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        effective_at = source.observed_at or source.published_at
        threshold = self._thresholds.get(source.evidence_type)
        if effective_at is None or threshold is None:
            freshness = FreshnessStatus.UNKNOWN
        elif source.retrieved_at.astimezone(UTC) - effective_at.astimezone(UTC) <= threshold:
            freshness = FreshnessStatus.FRESH
        else:
            freshness = FreshnessStatus.STALE
        return Evidence(
            evidence_id=source.evidence_id,
            security=source.security,
            evidence_type=source.evidence_type,
            provider=source.provider,
            source_reference=source.source_reference,
            observed_at=source.observed_at,
            published_at=source.published_at,
            retrieved_at=source.retrieved_at,
            payload_hash=digest,
            payload=_freeze_object(normalized_payload),
            freshness=freshness.value,
            entitlement=_freeze_object(normalized_entitlement),
        )


class EvidenceTrustPolicy:
    def __init__(
        self,
        *,
        allowed_providers: Mapping[str, frozenset[str]],
        require_fresh: frozenset[str] = frozenset(),
    ) -> None:
        self._allowed_providers = dict(allowed_providers)
        self._require_fresh = require_fresh

    def assess(self, evidence: Sequence[Evidence]) -> EvidenceAssessment:
        accepted: list[Evidence] = []
        rejected: list[str] = []
        gaps: list[str] = []
        for item in evidence:
            allowed = self._allowed_providers.get(item.evidence_type, frozenset())
            if item.provider not in allowed:
                rejected.append(item.evidence_id)
                gaps.append(f"{item.evidence_type}: provider 未获批准")
                continue
            if (
                item.evidence_type in self._require_fresh
                and item.freshness != FreshnessStatus.FRESH.value
            ):
                rejected.append(item.evidence_id)
                gaps.append(f"{item.evidence_type}: evidence 已过期或时效未知")
                continue
            accepted.append(item)

        conflicts = detect_conflicts(accepted)
        conflicted_ids = {item for conflict in conflicts for item in conflict.evidence_ids}
        accepted_ids = tuple(
            item.evidence_id for item in accepted if item.evidence_id not in conflicted_ids
        )
        rejected.extend(item.evidence_id for item in accepted if item.evidence_id in conflicted_ids)
        gaps.extend(f"{conflict.evidence_type}: provider evidence 冲突" for conflict in conflicts)
        return EvidenceAssessment(accepted_ids, tuple(rejected), conflicts, tuple(gaps))


def detect_conflicts(evidence: Sequence[Evidence]) -> tuple[EvidenceConflict, ...]:
    groups: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for item in evidence:
        groups[(item.security.symbol, item.evidence_type)].append(item)

    conflicts: list[EvidenceConflict] = []
    for (_, evidence_type), items in groups.items():
        unique_payloads = {item.payload_hash for item in items}
        if len(items) > 1 and len(unique_payloads) > 1:
            conflicts.append(
                EvidenceConflict(evidence_type, tuple(item.evidence_id for item in items))
            )
    return tuple(conflicts)


def validate_claim_citations(claims: Sequence[Claim], evidence: Sequence[Evidence]) -> None:
    known = {item.evidence_id for item in evidence}
    for claim in claims:
        if not claim.evidence_ids:
            raise ValueError(f"重要主张缺少 evidence: {claim.text}")
        unknown = set(claim.evidence_ids) - known
        if unknown:
            raise ValueError(f"主张引用未知 evidence: {', '.join(sorted(unknown))}")
