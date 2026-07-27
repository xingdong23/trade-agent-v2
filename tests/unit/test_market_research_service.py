"""证券/主题研究 assembly、citation、缺口与安全边界测试。"""

from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.market_research.application import (
    ResearchAssemblyService,
    ResearchSafetyError,
    SecurityResearchDraft,
)
from trade_agent.capabilities.market_research.contracts import Market, SecurityId
from trade_agent.capabilities.market_research.domain.evidence import EvidenceAssessment
from trade_agent.capabilities.market_research.domain.models import Evidence
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSectionKind,
    ThemeCandidate,
)


def _security(symbol: str = "NVDA") -> SecurityId:
    return SecurityId(Market.US, "NASDAQ", symbol, symbol)


def _evidence(evidence_id: str = "e-1") -> Evidence:
    return Evidence(
        evidence_id,
        _security(),
        "quote",
        "provider-a",
        f"quote:{evidence_id}",
        datetime(2026, 7, 27, tzinfo=UTC),
        None,
        datetime(2026, 7, 27, tzinfo=UTC),
        "0" * 64,
        {"price": 120.0},
        "fresh",
    )


def _assessment(*, accepted: tuple[str, ...] = ("e-1",)) -> EvidenceAssessment:
    return EvidenceAssessment(accepted, (), (), ())


def test_security_research_keeps_citations_and_explicit_missing_sections() -> None:
    artifact = ResearchAssemblyService().assemble_security(
        SecurityResearchDraft(
            "research-1",
            "owner-a",
            _security(),
            {
                ResearchSectionKind.PRICE_VOLUME: (
                    ResearchClaim("最新报价为 120 美元", ("e-1",), "high"),
                ),
                ResearchSectionKind.RISKS: (ResearchClaim("价格波动较高", ("e-1",), "medium"),),
                ResearchSectionKind.INVALIDATION: (
                    ResearchClaim("跌破关键区间则假设失效", ("e-1",), "medium"),
                ),
            },
        ),
        evidence=(_evidence(),),
        assessment=_assessment(),
    )
    assert artifact.evidence[0].evidence_id == "e-1"
    assert artifact.confidence == "low"
    assert "缺少 fundamentals 分析" in artifact.gaps


def test_research_rejects_unknown_or_untrusted_citations_and_unsafe_claims() -> None:
    service = ResearchAssemblyService()
    with pytest.raises(ValueError, match="未知 evidence"):
        service.assemble_security(
            SecurityResearchDraft(
                "research-1",
                "owner-a",
                _security(),
                {
                    ResearchSectionKind.RISKS: (
                        ResearchClaim("有数字但来源未被接受", ("e-1",), "low"),
                    )
                },
            ),
            evidence=(_evidence(),),
            assessment=_assessment(accepted=()),
        )
    with pytest.raises(ResearchSafetyError, match="禁止声明"):
        service.assemble_security(
            SecurityResearchDraft(
                "research-2",
                "owner-a",
                _security(),
                {ResearchSectionKind.RISKS: (ResearchClaim("该股票保证收益", ("e-1",), "high"),)},
            ),
            evidence=(_evidence(),),
            assessment=_assessment(),
        )


def test_theme_research_is_watchlist_proposal_only_and_requires_sources() -> None:
    service = ResearchAssemblyService()
    artifact = service.assemble_theme(
        artifact_id="theme-1",
        owner_id="owner-a",
        theme="AI 算力",
        candidates=(ThemeCandidate("GPU", _security(), ("e-1",), "生态可能形成护城河", ("竞争",)),),
        evidence=(_evidence(),),
        assessment=_assessment(),
    )
    assert artifact.watchlist_proposal_only
    assert artifact.candidates[0].role == "GPU"
    with pytest.raises(ValueError, match="缺少 supporting source"):
        service.assemble_theme(
            artifact_id="theme-2",
            owner_id="owner-a",
            theme="无来源主题",
            candidates=(ThemeCandidate("候选", _security(), (), "假设", ()),),
            evidence=(_evidence(),),
            assessment=_assessment(),
        )
