"""证券与主题研究的结构化 artifact、主张和安全约束。"""

from dataclasses import dataclass
from enum import StrEnum

from .evidence import Claim, validate_claim_citations
from .models import Evidence, SecurityId


class ResearchSectionKind(StrEnum):
    PRICE_VOLUME = "price_volume"
    TECHNICAL_LEVELS = "technical_levels"
    FUNDAMENTALS = "fundamentals"
    CATALYSTS = "catalysts"
    RISKS = "risks"
    ASSUMPTIONS = "assumptions"
    INVALIDATION = "invalidation"


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    text: str
    evidence_ids: tuple[str, ...]
    confidence: str


@dataclass(frozen=True, slots=True)
class ResearchSection:
    kind: ResearchSectionKind
    claims: tuple[ResearchClaim, ...]
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecurityResearchArtifact:
    artifact_id: str
    owner_id: str
    version: int
    security: SecurityId
    sections: tuple[ResearchSection, ...]
    evidence: tuple[Evidence, ...]
    gaps: tuple[str, ...]
    confidence: str


@dataclass(frozen=True, slots=True)
class ThemeCandidate:
    role: str
    security: SecurityId
    evidence_ids: tuple[str, ...]
    moat_hypothesis: str
    risks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThemeResearchArtifact:
    artifact_id: str
    owner_id: str
    version: int
    theme: str
    candidates: tuple[ThemeCandidate, ...]
    evidence: tuple[Evidence, ...]
    gaps: tuple[str, ...]
    watchlist_proposal_only: bool = True


def validate_research_claims(
    sections: tuple[ResearchSection, ...], evidence: tuple[Evidence, ...]
) -> None:
    claims = tuple(
        Claim(claim.text, claim.evidence_ids) for section in sections for claim in section.claims
    )
    validate_claim_citations(claims, evidence)


def validate_theme_candidates(
    candidates: tuple[ThemeCandidate, ...], evidence: tuple[Evidence, ...]
) -> None:
    known = {item.evidence_id for item in evidence}
    for candidate in candidates:
        if not candidate.evidence_ids:
            raise ValueError(f"主题候选 {candidate.security.symbol} 缺少 supporting source")
        unknown = set(candidate.evidence_ids) - known
        if unknown:
            raise ValueError(f"主题候选引用未知 evidence: {', '.join(sorted(unknown))}")
