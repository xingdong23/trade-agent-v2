"""美股解析和 evidence 可信链路的领域测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from trade_agent.capabilities.market_research.application import (
    SecurityResolver,
    security_resolution_copy_from_settings,
)
from trade_agent.capabilities.market_research.contracts import (
    Market,
    SecurityId,
    SecurityResolutionStatus,
)
from trade_agent.capabilities.market_research.domain.evidence import (
    Claim,
    EvidenceConflictRule,
    EvidenceFactory,
    EvidenceInput,
    EvidenceTrustPolicy,
    FreshnessStatus,
    detect_conflicts,
    validate_claim_citations,
)
from trade_agent.core.config import MarketSettings


def _nvda(exchange: str = "NASDAQ") -> SecurityId:
    return SecurityId(Market.US, exchange, "NVDA", "NVIDIA Corporation")


def test_resolver_handles_unique_ambiguous_missing_and_non_us_inputs() -> None:
    market_hints = frozenset({"US", "USA", "NASDAQ", "NYSE"})
    copy = security_resolution_copy_from_settings(MarketSettings())
    resolver = SecurityResolver(
        (_nvda(), SecurityId(Market.US, "NYSE", "BRK.B", "Berkshire")),
        supported_market_hints=market_hints,
        copy=copy,
    )
    assert resolver.resolve("nvda").security == _nvda()
    assert resolver.resolve("unknown").status is SecurityResolutionStatus.NOT_FOUND
    assert (
        resolver.resolve("0700", market_hint="HK").status
        is SecurityResolutionStatus.UNSUPPORTED_MARKET
    )

    ambiguous = SecurityResolver(
        (_nvda(), _nvda("NYSE")),
        supported_market_hints=market_hints,
        copy=copy,
    ).resolve("NVDA")
    assert ambiguous.status is SecurityResolutionStatus.AMBIGUOUS
    assert len(ambiguous.candidates) == 2


def test_evidence_is_hashed_and_marked_stale_without_mutating_input() -> None:
    now = datetime.now(UTC)
    raw_payload = {"price": 120.5}
    factory = EvidenceFactory({"quote": timedelta(minutes=1)})
    evidence = factory.create(
        EvidenceInput(
            evidence_id="e-1",
            security=_nvda(),
            evidence_type="quote",
            provider="provider-a",
            source_reference="quote:NVDA",
            observed_at=now - timedelta(minutes=5),
            published_at=None,
            retrieved_at=now,
            payload=raw_payload,
            entitlement={"redistributable": False},
        )
    )

    raw_payload["price"] = 999.0
    assert evidence.payload == {"price": 120.5}
    with pytest.raises(TypeError):
        evidence.payload["price"] = 999.0
    assert evidence.freshness == FreshnessStatus.STALE
    assert len(evidence.payload_hash) == 64


def test_conflicting_provider_payloads_are_retained_and_detected() -> None:
    now = datetime.now(UTC)
    factory = EvidenceFactory({"quote": timedelta(minutes=10)})
    first = factory.create(
        EvidenceInput(
            evidence_id="e-1",
            security=_nvda(),
            evidence_type="quote",
            provider="provider-a",
            source_reference="a:NVDA",
            observed_at=now,
            published_at=None,
            retrieved_at=now,
            payload={"price": 120.0},
            entitlement={},
        )
    )
    second = factory.create(
        EvidenceInput(
            evidence_id="e-2",
            security=_nvda(),
            evidence_type="quote",
            provider="provider-b",
            source_reference="b:NVDA",
            observed_at=now,
            published_at=None,
            retrieved_at=now,
            payload={"price": 121.0},
            entitlement={},
        )
    )

    conflict_rules = {"quote": EvidenceConflictRule(("price",), 0.25)}
    conflicts = detect_conflicts((first, second), conflict_rules)
    assert conflicts[0].evidence_ids == ("e-1", "e-2")

    assessment = EvidenceTrustPolicy(
        allowed_providers={"quote": frozenset({"provider-a", "provider-b"})},
        conflict_rules=conflict_rules,
        require_fresh=frozenset({"quote"}),
    ).assess((first, second))
    assert assessment.accepted_evidence_ids == ()
    assert set(assessment.rejected_evidence_ids) == {"e-1", "e-2"}
    assert "provider evidence 冲突" in assessment.gaps[0]

    same_fact_with_provider_metadata = replace(
        second,
        payload={"price": 120.0, "provider_sequence": 99},
        payload_hash="different-raw-payload-hash",
    )
    assert detect_conflicts((first, same_fact_with_provider_metadata), conflict_rules) == ()


def test_claim_validator_rejects_missing_and_unknown_citations() -> None:
    now = datetime.now(UTC)
    evidence = EvidenceFactory({}).create(
        EvidenceInput(
            evidence_id="e-1",
            security=_nvda(),
            evidence_type="filing",
            provider="sec",
            source_reference="sec:filing",
            observed_at=None,
            published_at=now,
            retrieved_at=now,
            payload={"revenue": 1},
            entitlement={},
        )
    )
    validate_claim_citations((Claim("有来源的主张", ("e-1",)),), (evidence,))

    with pytest.raises(ValueError, match="缺少 evidence"):
        validate_claim_citations((Claim("无来源主张", ()),), (evidence,))
    with pytest.raises(ValueError, match="未知 evidence"):
        validate_claim_citations((Claim("错误来源", ("missing",)),), (evidence,))
