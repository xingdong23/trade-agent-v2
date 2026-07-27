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


class ResearchSafetyClass(StrEnum):
    """供本地安全策略判断研究主张性质的结构化标签。"""

    ANALYSIS = "analysis"
    RETURN_GUARANTEE = "return_guarantee"
    EXECUTION_CLAIM = "execution_claim"


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """表示一条必须绑定证据的研究主张。

    Attributes:
        text: 面向用户展示的主张文本。
        evidence_ids: 支撑该主张的 evidence 标识集合。
        confidence: 该主张当前的置信度分级。
        safety_class: 主张的结构化安全分类，不从展示文本推断。
    """

    text: str
    evidence_ids: tuple[str, ...]
    confidence: str
    safety_class: ResearchSafetyClass = ResearchSafetyClass.ANALYSIS


@dataclass(frozen=True, slots=True)
class ResearchSection:
    """把同类研究主张聚合为一个结构化章节。

    Attributes:
        kind: 章节主题，例如技术位、风险或催化因素。
        claims: 当前章节下的全部研究主张。
        gaps: 当前章节仍缺失或不可得的信息提示。
    """

    kind: ResearchSectionKind
    claims: tuple[ResearchClaim, ...]
    gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecurityResearchArtifact:
    """由证据约束的单证券研究产物。

    Attributes:
        artifact_id: 研究产物稳定标识。
        owner_id: 产物归属用户。
        version: 研究产物版本。
        security: 被研究的规范证券。
        sections: 按主题拆分的研究章节集合。
        evidence: 本次研究直接引用的证据快照集合。
        gaps: 对整份研究仍然成立的数据缺口。
        confidence: 对整体研究结论的置信度分级。
        assembly_policy_version: 生成章节缺口与置信度所用的策略版本。
    """

    artifact_id: str
    owner_id: str
    version: int
    security: SecurityId
    sections: tuple[ResearchSection, ...]
    evidence: tuple[Evidence, ...]
    gaps: tuple[str, ...]
    confidence: str
    assembly_policy_version: str


@dataclass(frozen=True, slots=True)
class ThemeCandidate:
    """表示主题研究中的一个候选证券与其角色假设。

    Attributes:
        role: 该证券在主题、行业或产业链中的角色描述。
        security: 候选规范证券。
        evidence_ids: 支撑该角色判断的证据标识集合。
        moat_hypothesis: 对竞争优势或位置的简要假设。
        risks: 当前候选项需要额外关注的主要风险。
        safety_classes: 候选描述涉及的结构化安全分类。
    """

    role: str
    security: SecurityId
    evidence_ids: tuple[str, ...]
    moat_hypothesis: str
    risks: tuple[str, ...]
    safety_classes: tuple[ResearchSafetyClass, ...] = (ResearchSafetyClass.ANALYSIS,)


@dataclass(frozen=True, slots=True)
class ThemeResearchArtifact:
    """由主题研究生成的候选证券集合产物。

    Attributes:
        artifact_id: 主题研究产物稳定标识。
        owner_id: 产物归属用户。
        version: 当前产物版本。
        theme: 本次研究的主题或产业链名称。
        candidates: 主题下筛出的候选证券集合。
        evidence: 支撑主题判断的证据快照集合。
        gaps: 当前主题研究仍然存在的信息缺口。
        watchlist_proposal_only: 是否仅允许提出 watchlist 建议而不直接写入。
        assembly_policy_version: 生成该产物所用的研究策略版本。
    """

    artifact_id: str
    owner_id: str
    version: int
    theme: str
    candidates: tuple[ThemeCandidate, ...]
    evidence: tuple[Evidence, ...]
    gaps: tuple[str, ...]
    watchlist_proposal_only: bool
    assembly_policy_version: str


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
