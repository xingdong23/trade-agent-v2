"""证券/主题研究 assembly、citation、缺口与安全边界测试。"""

from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.market_research.application import (
    ConfidenceBand,
    ResearchAssemblyPolicy,
    ResearchAssemblyService,
    ResearchSafetyError,
    SecurityResearchDraft,
)
from trade_agent.capabilities.market_research.contracts import Market, SecurityId
from trade_agent.capabilities.market_research.domain.evidence import EvidenceAssessment
from trade_agent.capabilities.market_research.domain.models import Evidence
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSafetyClass,
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


def _policy(
    required_sections: tuple[ResearchSectionKind, ...] | None = None,
) -> ResearchAssemblyPolicy:
    return ResearchAssemblyPolicy(
        "us-equity-research.v1",
        required_sections
        or (
            ResearchSectionKind.PRICE_VOLUME,
            ResearchSectionKind.TECHNICAL_LEVELS,
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.CATALYSTS,
            ResearchSectionKind.RISKS,
            ResearchSectionKind.ASSUMPTIONS,
            ResearchSectionKind.INVALIDATION,
        ),
        (ConfidenceBand(0, "high"), ConfidenceBand(2, "medium"), ConfidenceBand(None, "low")),
        "缺少 {section} 分析",
        True,
    )


def test_security_research_keeps_citations_and_explicit_missing_sections() -> None:
    artifact = ResearchAssemblyService(_policy()).assemble_security(
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
    service = ResearchAssemblyService(_policy())
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
    with pytest.raises(ResearchSafetyError, match="禁止安全分类"):
        service.assemble_security(
            SecurityResearchDraft(
                "research-2",
                "owner-a",
                _security(),
                {
                    ResearchSectionKind.RISKS: (
                        ResearchClaim(
                            "模型输出了不允许的确定性承诺",
                            ("e-1",),
                            "high",
                            ResearchSafetyClass.RETURN_GUARANTEE,
                        ),
                    )
                },
            ),
            evidence=(_evidence(),),
            assessment=_assessment(),
        )


def test_research_safety_does_not_parse_display_text_keywords() -> None:
    artifact = ResearchAssemblyService(_policy()).assemble_security(
        SecurityResearchDraft(
            "research-negation",
            "owner-a",
            _security(),
            {
                ResearchSectionKind.RISKS: (
                    ResearchClaim("本系统不保证收益，也没有执行下单。", ("e-1",), "high"),
                )
            },
        ),
        evidence=(_evidence(),),
        assessment=_assessment(),
    )

    assert artifact.sections[0].claims[0].safety_class is ResearchSafetyClass.ANALYSIS


def test_theme_research_is_watchlist_proposal_only_and_requires_sources() -> None:
    service = ResearchAssemblyService(_policy())
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


def test_research_sections_and_confidence_are_driven_by_injected_policy() -> None:
    policy = _policy((ResearchSectionKind.FUNDAMENTALS, ResearchSectionKind.RISKS))
    artifact = ResearchAssemblyService(policy).assemble_security(
        SecurityResearchDraft(
            "research-configured",
            "owner-a",
            _security(),
            {ResearchSectionKind.RISKS: (ResearchClaim("价格波动较高", ("e-1",), "medium"),)},
        ),
        evidence=(_evidence(),),
        assessment=_assessment(),
    )

    assert artifact.gaps == ("缺少 fundamentals 分析",)
    assert artifact.confidence == "medium"
    assert artifact.assembly_policy_version == "us-equity-research.v1"
