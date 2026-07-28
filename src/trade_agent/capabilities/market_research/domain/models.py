"""Market Research capability 拥有的证券、证据和研究产物模型。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trade_agent.core.llm.contracts import JsonValue

type FrozenJsonValue = (
    str
    | int
    | float
    | bool
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
    | None
)


class Market(StrEnum):
    """首版允许研究的市场枚举。

    Attributes:
        US: 美国交易所上市证券市场。

    Invariants:
        - 首版只允许美国市场，其他市场必须返回 ``unsupported_market``。
    """

    US = "US"


class SecurityResolutionStatus(StrEnum):
    """证券解析的确定性结果状态。

    Attributes:
        RESOLVED: 输入唯一解析为一个规范证券。
        AMBIGUOUS: 输入对应多个候选证券，需要 HITL 澄清。
        NOT_FOUND: provider 无法找到对应证券。
        UNSUPPORTED_MARKET: 证券存在但不属于首版支持的美国市场。
    """

    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNSUPPORTED_MARKET = "unsupported_market"


@dataclass(frozen=True, slots=True)
class SecurityId:
    """系统内部唯一、规范化的美股证券标识。

    Attributes:
        market: 首版固定为美国市场。
        exchange: NASDAQ、NYSE 等美国交易所代码。
        symbol: 交易所内证券代码。
        display_name: 面向用户的公司或证券名称。

    Invariants:
        - Market 必须为 US，且 exchange、symbol、display_name 均非空。
    """

    market: Market
    exchange: str
    symbol: str
    display_name: str

    def __post_init__(self) -> None:
        if self.market is not Market.US:
            raise ValueError("首版只允许美股证券")
        if not self.exchange or not self.symbol or not self.display_name:
            raise ValueError("规范证券必须包含交易所、symbol 和展示名称")


@dataclass(frozen=True, slots=True)
class SecurityResolution:
    """用户输入解析为规范证券后的结果。

    Attributes:
        status: 唯一命中、歧义、未找到或不支持市场。
        candidates: 需要用户选择或最终命中的候选证券。
        message: 可展示的解析说明。

    Invariants:
        - ``security`` 只在状态为 resolved 且恰有一个候选时可用。
    """

    status: SecurityResolutionStatus
    candidates: tuple[SecurityId, ...] = ()
    message: str = ""

    @property
    def security(self) -> SecurityId | None:
        if self.status is SecurityResolutionStatus.RESOLVED and len(self.candidates) == 1:
            return self.candidates[0]
        return None


@dataclass(frozen=True, slots=True)
class Evidence:
    """可追溯、带时效和授权信息的研究证据快照。

    Attributes:
        evidence_id: 证据稳定标识。
        security: 证据对应的规范证券。
        evidence_type: quote、fundamentals、news 等证据类别。
        provider: 数据提供方标识。
        source_reference: 可审计的来源引用。
        observed_at: 数据实际观察时点。
        published_at: 原始来源发布时间。
        retrieved_at: 系统取得数据的时间。
        payload_hash: 规范化 payload 完整性摘要。
        payload: 冻结后的证据内容。
        freshness: 新鲜度分类。
        entitlement: 数据授权与展示约束。

    Invariants:
        - 所有存在的 datetime 必须带时区。
        - Payload 创建后不可变，研究结论只引用 evidence_id。
    """

    evidence_id: str
    security: SecurityId
    evidence_type: str
    provider: str
    source_reference: str
    observed_at: datetime | None
    published_at: datetime | None
    retrieved_at: datetime
    payload_hash: str
    payload: Mapping[str, FrozenJsonValue]
    freshness: str
    entitlement: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at 必须包含时区")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise ValueError("observed_at 必须包含时区")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at 必须包含时区")


@dataclass(frozen=True, slots=True)
class ResearchArtifact:
    """由证据约束的版本化证券研究产物。

    Attributes:
        artifact_id: 研究产物稳定标识。
        owner_id: 资源所有者。
        security: 被研究的规范证券。
        version: 研究产物版本。
        evidence_ids: 支撑结论的证据快照标识。
        claims: 已通过 citation 门禁的研究主张。
        gaps: 明确披露的数据或证据缺口。
        metadata: 不参与核心规则的扩展字段。
    """

    artifact_id: str
    owner_id: str
    security: SecurityId
    version: int
    evidence_ids: tuple[str, ...]
    claims: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
