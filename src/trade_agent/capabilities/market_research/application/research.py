"""有证据约束的证券和主题研究 application service。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from trade_agent.capabilities.market_research.domain.evidence import EvidenceAssessment
from trade_agent.capabilities.market_research.domain.models import Evidence, SecurityId
from trade_agent.capabilities.market_research.domain.research import (
    ResearchClaim,
    ResearchSafetyClass,
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


class ResearchSafetyPolicy(Protocol):
    """研究内容进入 artifact 前必须实现的结构化安全策略。"""

    policy_version: str

    def validate_claims(self, claims: Sequence[ResearchClaim]) -> None:
        """校验证券研究主张的结构化安全分类。"""

    def validate_candidates(self, candidates: Sequence[ThemeCandidate]) -> None:
        """校验主题候选描述的结构化安全分类。"""


class ResearchSafetyValidator:
    """默认版本化安全策略，只消费结构化标签而不解析展示文本。

    Notes:
        关键词扫描无法正确理解否定、同义表达和多语言文本，因此不能作为控制流
        边界。上游结构化输出必须显式标注主张分类；应用层只依据枚举判定。
    """

    policy_version = "research-safety.v1"
    _FORBIDDEN = frozenset(
        {ResearchSafetyClass.RETURN_GUARANTEE, ResearchSafetyClass.EXECUTION_CLAIM}
    )

    def validate_claims(self, claims: Sequence[ResearchClaim]) -> None:
        for claim in claims:
            self._validate_classes((claim.safety_class,))

    def validate_candidates(self, candidates: Sequence[ThemeCandidate]) -> None:
        for candidate in candidates:
            self._validate_classes(candidate.safety_classes)

    def _validate_classes(self, classes: Sequence[ResearchSafetyClass]) -> None:
        forbidden = self._FORBIDDEN.intersection(classes)
        if forbidden:
            labels = ", ".join(sorted(item.value for item in forbidden))
            raise ResearchSafetyError(f"研究内容包含禁止安全分类: {labels}")


@dataclass(frozen=True, slots=True)
class ConfidenceBand:
    """把最大 gap 数量映射为研究置信度标签。"""

    maximum_gap_count: int | None
    label: str

    def __post_init__(self) -> None:
        if self.maximum_gap_count is not None and self.maximum_gap_count < 0:
            raise ValueError("confidence band 的最大 gap 数量不能为负")
        if not self.label.strip():
            raise ValueError("confidence band label 不能为空")


@dataclass(frozen=True, slots=True)
class ResearchAssemblyPolicy:
    """定义研究章节、缺口文案、置信度和 watchlist 建议策略。

    Attributes:
        policy_version: 可写入 artifact lineage 的稳定策略版本。
        required_sections: 当前研究模板要求的章节集合。
        confidence_bands: 按 gap 数量升序匹配的置信度区间，最后一项必须无上限。
        missing_section_template: 包含 `{section}` 占位符的缺失章节文案。
        watchlist_proposal_only: 主题研究是否只能生成待审批建议。
    """

    policy_version: str
    required_sections: tuple[ResearchSectionKind, ...]
    confidence_bands: tuple[ConfidenceBand, ...]
    missing_section_template: str
    watchlist_proposal_only: bool

    def __post_init__(self) -> None:
        if not self.policy_version.strip() or "{section}" not in self.missing_section_template:
            raise ValueError("research assembly policy 版本或缺失章节模板无效")
        if len(set(self.required_sections)) != len(self.required_sections):
            raise ValueError("research required_sections 不能重复")
        if not self.confidence_bands or self.confidence_bands[-1].maximum_gap_count is not None:
            raise ValueError("research confidence_bands 最后一项必须覆盖无上限 gap")
        finite_limits = tuple(
            band.maximum_gap_count
            for band in self.confidence_bands
            if band.maximum_gap_count is not None
        )
        if finite_limits != tuple(sorted(finite_limits)):
            raise ValueError("research confidence_bands 必须按 gap 数量升序排列")

    def confidence_for(self, gap_count: int) -> str:
        """按显式区间返回研究整体置信度。"""

        for band in self.confidence_bands:
            if band.maximum_gap_count is None or gap_count <= band.maximum_gap_count:
                return band.label
        raise AssertionError("无上限 confidence band 保证所有 gap 数量均有结果")


@dataclass(frozen=True, slots=True)
class SecurityResearchDraft:
    artifact_id: str
    owner_id: str
    security: SecurityId
    claims_by_section: Mapping[ResearchSectionKind, tuple[ResearchClaim, ...]]
    gaps: tuple[str, ...] = ()


class ResearchAssemblyService:
    def __init__(
        self,
        assembly_policy: ResearchAssemblyPolicy,
        safety: ResearchSafetyPolicy | None = None,
    ) -> None:
        self._assembly_policy = assembly_policy
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
        claims = tuple(claim for section in sections for claim in section.claims)
        self._safety.validate_claims(claims)
        validate_research_claims(sections, tuple(accepted.values()))
        missing_sections = tuple(
            self._assembly_policy.missing_section_template.format(section=kind.value)
            for kind in self._assembly_policy.required_sections
            if kind not in draft.claims_by_section
        )
        gaps = tuple(dict.fromkeys((*draft.gaps, *assessment.gaps, *missing_sections)))
        confidence = self._assembly_policy.confidence_for(len(gaps))
        return SecurityResearchArtifact(
            draft.artifact_id,
            draft.owner_id,
            version,
            draft.security,
            sections,
            tuple(accepted.values()),
            gaps,
            confidence,
            self._assembly_policy.policy_version,
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
        self._safety.validate_candidates(normalized_candidates)
        validate_theme_candidates(normalized_candidates, accepted)
        return ThemeResearchArtifact(
            artifact_id,
            owner_id,
            version,
            theme,
            normalized_candidates,
            accepted,
            assessment.gaps,
            self._assembly_policy.watchlist_proposal_only,
            self._assembly_policy.policy_version,
        )
