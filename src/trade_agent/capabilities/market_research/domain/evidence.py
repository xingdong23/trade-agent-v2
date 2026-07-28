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
    """证据时效性判定的稳定枚举。

    Attributes:
        FRESH: 证据在配置阈值内，允许作为新鲜数据使用。
        STALE: 证据超出配置阈值，时效要求严格时应被拒绝。
        UNKNOWN: 缺少观测时间或未配置阈值，无法判断是否新鲜。

    Invariants:
        - 枚举值直接写入 ``Evidence.freshness``，属于持久化协议字段。
    """

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    """创建不可变证据快照前的原始输入。

    Attributes:
        evidence_id: 证据稳定标识。
        security: 证据对应的规范证券。
        evidence_type: 证据类型，例如 quote 或 fundamentals。
        provider: 提供该证据的上游 provider 标识。
        source_reference: 上游系统中的来源引用。
        observed_at: 数据实际观测时间；未知时可为空。
        published_at: 数据发布时间；未知时可为空。
        retrieved_at: 当前系统拉取该数据的时间。
        payload: 规范化前的原始 JSON 负载。
        entitlement: 读取该证据所需的授权范围元数据。

    Invariants:
        - 本模型承载进入不可变快照前的原始输入，不应被下游原地修改。
        - payload 与 entitlement 必须能规范化为 JSON object。
    """

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
    """表示同类证据之间的冲突集合。

    Attributes:
        evidence_type: 发生冲突的证据类型。
        evidence_ids: 参与冲突判定的证据标识集合。

    Invariants:
        - evidence_ids 只记录同一证券、同一 evidence_type 下参与比较的证据。
    """

    evidence_type: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceConflictRule:
    """定义同类 evidence 中哪些事实字段需要做实质冲突比较。

    Attributes:
        value_paths: 支持点号嵌套的事实字段路径。
        absolute_tolerance: 数值字段允许的绝对误差。

    Invariants:
        - 至少声明一个事实字段。
        - 数值容差不能为负。
    """

    value_paths: tuple[str, ...]
    absolute_tolerance: float

    def __post_init__(self) -> None:
        if not self.value_paths or any(not path.strip() for path in self.value_paths):
            raise ValueError("evidence conflict rule 必须声明事实字段")
        if self.absolute_tolerance < 0:
            raise ValueError("evidence conflict 数值容差不能为负")


@dataclass(frozen=True, slots=True)
class Claim:
    """表示一条必须绑定证据的事实主张。

    Attributes:
        text: 主张文本。
        evidence_ids: 支撑该主张的证据标识集合。

    Invariants:
        - 每条主张都必须引用至少一个下游可校验的证据标识。
    """

    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """表示 trust policy 对一批证据的评估结果。

    Attributes:
        accepted_evidence_ids: 可继续进入下游研究或计划的证据标识集合。
        rejected_evidence_ids: 因授权、时效或冲突被拒绝的证据标识集合。
        conflicts: 同类证据之间的结构化冲突信息。
        gaps: 应向用户暴露的数据缺口或拒绝原因。

    Invariants:
        - accepted_evidence_ids 与 rejected_evidence_ids 表示同一轮评估的互斥结果。
        - conflicts 与 gaps 必须能解释所有因冲突被拒绝的证据。
    """

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
        conflict_rules: Mapping[str, EvidenceConflictRule],
        require_fresh: frozenset[str] = frozenset(),
    ) -> None:
        self._allowed_providers = dict(allowed_providers)
        self._conflict_rules = dict(conflict_rules)
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

        conflicts = detect_conflicts(accepted, self._conflict_rules)
        conflicted_ids = {item for conflict in conflicts for item in conflict.evidence_ids}
        accepted_ids = tuple(
            item.evidence_id for item in accepted if item.evidence_id not in conflicted_ids
        )
        rejected.extend(item.evidence_id for item in accepted if item.evidence_id in conflicted_ids)
        gaps.extend(f"{conflict.evidence_type}: provider evidence 冲突" for conflict in conflicts)
        return EvidenceAssessment(accepted_ids, tuple(rejected), conflicts, tuple(gaps))


def detect_conflicts(
    evidence: Sequence[Evidence],
    rules: Mapping[str, EvidenceConflictRule],
) -> tuple[EvidenceConflict, ...]:
    """按注入规则比较事实字段，忽略 provider 私有 metadata 差异。"""

    groups: dict[tuple[str, str, str, str], list[Evidence]] = defaultdict(list)
    for item in evidence:
        groups[
            (
                item.security.market.value,
                item.security.exchange,
                item.security.symbol,
                item.evidence_type,
            )
        ].append(item)

    conflicts: list[EvidenceConflict] = []
    for (*_, evidence_type), items in groups.items():
        rule = rules.get(evidence_type)
        if rule is None or len(items) < 2:
            continue
        signatures = tuple(_conflict_signature(item, rule) for item in items)
        complete = tuple(signature for signature in signatures if signature is not None)
        if len(complete) > 1 and _has_material_difference(complete, rule.absolute_tolerance):
            conflicts.append(
                EvidenceConflict(evidence_type, tuple(item.evidence_id for item in items))
            )
    return tuple(conflicts)


def _conflict_signature(
    evidence: Evidence, rule: EvidenceConflictRule
) -> tuple[FrozenJsonValue, ...] | None:
    values: list[FrozenJsonValue] = []
    for path in rule.value_paths:
        value = _payload_at_path(evidence.payload, path)
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _payload_at_path(payload: Mapping[str, FrozenJsonValue], path: str) -> FrozenJsonValue:
    value: FrozenJsonValue = payload
    for segment in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _has_material_difference(
    signatures: Sequence[tuple[FrozenJsonValue, ...]], tolerance: float
) -> bool:
    baseline = signatures[0]
    for candidate in signatures[1:]:
        for left, right in zip(baseline, candidate, strict=True):
            if isinstance(left, int | float) and isinstance(right, int | float):
                if abs(float(left) - float(right)) > tolerance:
                    return True
            elif left != right:
                return True
    return False


def validate_claim_citations(claims: Sequence[Claim], evidence: Sequence[Evidence]) -> None:
    known = {item.evidence_id for item in evidence}
    for claim in claims:
        if not claim.evidence_ids:
            raise ValueError(f"重要主张缺少 evidence: {claim.text}")
        unknown = set(claim.evidence_ids) - known
        if unknown:
            raise ValueError(f"主张引用未知 evidence: {', '.join(sorted(unknown))}")
