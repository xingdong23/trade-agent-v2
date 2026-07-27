"""有证据约束的证券和主题研究 application service。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from trade_agent.capabilities.market_research.domain.evidence import EvidenceAssessment
from trade_agent.capabilities.market_research.domain.models import Evidence, SecurityId
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSection,
    ResearchSectionKind,
    SecurityResearchArtifact,
    ThemeCandidate,
    ThemeResearchArtifact,
    validate_research_claims,
    validate_theme_candidates,
)


class ResearchSafetyError(ValueError):
    """研究内容跨越决策辅助安全边界。"""


class ResearchSafetyValidator:
    _FORBIDDEN = (
        "保证收益",
        "稳赚",
        "必涨",
        "已下单",
        "已成交",
        "撤单成功",
        "broker sync",
        "account updated",
    )

    def validate_text(self, values: Sequence[str]) -> None:
        for value in values:
            lowered = value.lower()
            forbidden = next((item for item in self._FORBIDDEN if item in lowered), None)
            if forbidden is not None:
                raise ResearchSafetyError(f"研究内容包含禁止声明: {forbidden}")


@dataclass(frozen=True, slots=True)
class SecurityResearchDraft:
    artifact_id: str
    owner_id: str
    security: SecurityId
    claims_by_section: Mapping[ResearchSectionKind, tuple[ResearchClaim, ...]]
    gaps: tuple[str, ...] = ()


class ResearchAssemblyService:
    def __init__(self, safety: ResearchSafetyValidator | None = None) -> None:
        self._safety = safety or ResearchSafetyValidator()

    def assemble_security(
        self,
        draft: SecurityResearchDraft,
        *,
        evidence: Sequence[Evidence],
        assessment: EvidenceAssessment,
        version: int = 1,
    ) -> SecurityResearchArtifact:
        accepted = {
            item.evidence_id: item
            for item in evidence
            if item.evidence_id in assessment.accepted_evidence_ids
        }
        sections = tuple(
            ResearchSection(kind, tuple(claims)) for kind, claims in draft.claims_by_section.items()
        )
        texts = tuple(claim.text for section in sections for claim in section.claims)
        self._safety.validate_text(texts)
        validate_research_claims(sections, tuple(accepted.values()))
        required_kinds = {
            ResearchSectionKind.PRICE_VOLUME,
            ResearchSectionKind.TECHNICAL_LEVELS,
            ResearchSectionKind.FUNDAMENTALS,
            ResearchSectionKind.CATALYSTS,
            ResearchSectionKind.RISKS,
            ResearchSectionKind.ASSUMPTIONS,
            ResearchSectionKind.INVALIDATION,
        }
        missing_sections = tuple(
            f"缺少 {kind.value} 分析"
            for kind in required_kinds
            if kind not in draft.claims_by_section
        )
        gaps = tuple(dict.fromkeys((*draft.gaps, *assessment.gaps, *missing_sections)))
        confidence = "low" if gaps else "high"
        return SecurityResearchArtifact(
            draft.artifact_id,
            draft.owner_id,
            version,
            draft.security,
            sections,
            tuple(accepted.values()),
            gaps,
            confidence,
        )

    def assemble_theme(
        self,
        *,
        artifact_id: str,
        owner_id: str,
        theme: str,
        candidates: Sequence[ThemeCandidate],
        evidence: Sequence[Evidence],
        assessment: EvidenceAssessment,
        version: int = 1,
    ) -> ThemeResearchArtifact:
        accepted = tuple(
            item for item in evidence if item.evidence_id in assessment.accepted_evidence_ids
        )
        normalized_candidates = tuple(candidates)
        self._safety.validate_text(
            tuple(
                value
                for item in normalized_candidates
                for value in (item.moat_hypothesis, *item.risks)
            )
        )
        validate_theme_candidates(normalized_candidates, accepted)
        return ThemeResearchArtifact(
            artifact_id,
            owner_id,
            version,
            theme,
            normalized_candidates,
            accepted,
            assessment.gaps,
            True,
        )
