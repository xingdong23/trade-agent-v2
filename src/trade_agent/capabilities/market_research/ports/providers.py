"""美股研究依赖的 provider ports 与统一失败契约。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from trade_agent.capabilities.market_research.contracts import SecurityId, SecurityResolution
from trade_agent.core.llm.contracts import JsonValue


class ProviderErrorCode(StrEnum):
    """市场数据 provider 失败类型的稳定错误码。

    Attributes:
        RATE_LIMITED: 上游因频率限制拒绝请求，通常可稍后重试。
        TIMEOUT: 上游在时限内未返回结果。
        UNAVAILABLE: 上游服务不可用或暂时故障。
        UNAUTHORIZED: 当前请求缺少有效授权或 entitlement。
        INVALID_RESPONSE: 上游返回结构不合法、缺字段或语义不一致。
        UNSUPPORTED_MARKET: 请求超出当前 provider 支持的市场范围。

    Invariants:
        - 错误码是跨 provider 的统一契约，调用方依赖其稳定语义做恢复与提示。
    """

    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    INVALID_RESPONSE = "invalid_response"
    UNSUPPORTED_MARKET = "unsupported_market"


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class ProviderRequestContext:
    """单次 provider 调用共享的请求上下文。

    Attributes:
        correlation_id: 用于链路追踪与审计的稳定关联标识。
        as_of: 本次读取语义所对应的带时区时间点。
        entitlement_scope: 当前请求可消费的数据授权范围。

    Invariants:
        - as_of 必须包含时区，避免 provider 解释歧义。
    """

    correlation_id: str
    as_of: datetime
    entitlement_scope: str

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("provider request as_of 必须包含时区")


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """provider 返回的一条标准化观测结果。

    Attributes:
        provider: 产生该观测的 provider 标识。
        evidence_type: 该观测对应的证据类型。
        source_reference: 上游系统中的来源引用。
        observed_at: 数据被观测到的时间；未知时为空。
        published_at: 数据被发布的时间；未知时为空。
        retrieved_at: 当前系统拉取该观测的时间。
        payload: 标准化后的 JSON 负载。
        entitlement: 读取该观测所需的授权元数据。

    Invariants:
        - provider 与 source_reference 不能为空。
        - 所有时间字段一旦提供就必须包含时区。
    """

    provider: str
    evidence_type: str
    source_reference: str
    observed_at: datetime | None
    published_at: datetime | None
    retrieved_at: datetime
    payload: Mapping[str, JsonValue]
    entitlement: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.provider or not self.source_reference:
            raise ValueError("provider observation 必须包含 provider 与 source reference")
        for value in (self.observed_at, self.published_at, self.retrieved_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("provider observation 时间必须包含时区")


class SecurityLookupProvider(Protocol):
    """把自由输入解析为规范美国证券的 provider 协议。

    Contract:
        - 必须返回类型化解析状态，不得用异常文本表达歧义或非美股结果。
        - 请求必须遵守 ``ProviderRequestContext`` 的时间点与授权范围。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的真实美股数据 adapter。
    """

    async def resolve(self, query: str, context: ProviderRequestContext) -> SecurityResolution: ...


class QuoteProvider(Protocol):
    """读取单只规范证券报价观测的 provider 协议。

    Contract:
        - 返回值必须保留来源、拉取时间与 entitlement 元数据。
        - 不可用或未授权必须抛出带稳定错误码的 ``ProviderError``。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的真实行情 adapter。
    """

    async def quote(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> ProviderObservation: ...


class KlineProvider(Protocol):
    """读取指定时间区间 K 线观测序列的 provider 协议。

    Contract:
        - 返回序列必须受请求时间区间约束并保留 point-in-time 元数据。
        - provider 不能静默填补缺失交易日或伪造复权结果。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的真实行情 adapter。
    """

    async def klines(
        self,
        security: SecurityId,
        start: datetime,
        end: datetime,
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]: ...


class CorporateActionProvider(Protocol):
    """读取证券公司行动观测的 provider 协议。

    Contract:
        - 每条公司行动必须包含可追踪来源和发布时间。
        - 未知数据必须保持缺失，不能由 LLM 或 adapter 推断补齐。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的公司行动 adapter。
    """

    async def corporate_actions(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]: ...


class FundamentalsProvider(Protocol):
    """读取证券基本面与 SEC 观测的 provider 协议。

    Contract:
        - 返回值必须记录发布时间，避免未来数据进入历史决策时点。
        - 授权失败和上游不可用必须使用统一 ``ProviderError``。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的基本面 adapter。
    """

    async def fundamentals(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]: ...


class NewsSearchProvider(Protocol):
    """搜索主题或证券相关新闻观测的 provider 协议。

    Contract:
        - 返回结果必须包含发布、拉取时间和来源引用。
        - 搜索文本只用于外部检索，不能直接成为系统业务事实。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的新闻搜索 adapter。
    """

    async def search(
        self,
        query: str,
        securities: Sequence[SecurityId],
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]: ...


class NotificationProvider(Protocol):
    """投递市场研究相关通知的 provider 协议。

    Contract:
        - 返回稳定投递引用，不得宣称发生交易或成交。
        - recipient、模板和 payload 必须受当前请求授权范围约束。

    Implemented by:
        ``FakeMarketProvider`` 以及后续注册的通知 adapter。
    """

    async def deliver(
        self,
        *,
        recipient_id: str,
        template_id: str,
        payload: Mapping[str, JsonValue],
        context: ProviderRequestContext,
    ) -> str: ...
