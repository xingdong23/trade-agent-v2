"""美股研究依赖的 provider ports 与统一失败契约。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from trade_agent.capabilities.market_research.contracts import SecurityId, SecurityResolution
from trade_agent.core.llm.contracts import JsonValue


class ProviderErrorCode(StrEnum):
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
    correlation_id: str
    as_of: datetime
    entitlement_scope: str

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("provider request as_of 必须包含时区")


@dataclass(frozen=True, slots=True)
class ProviderObservation:
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
    async def resolve(self, query: str, context: ProviderRequestContext) -> SecurityResolution: ...


class QuoteProvider(Protocol):
    async def quote(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> ProviderObservation: ...


class KlineProvider(Protocol):
    async def klines(
        self,
        security: SecurityId,
        start: datetime,
        end: datetime,
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]: ...


class CorporateActionProvider(Protocol):
    async def corporate_actions(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]: ...


class FundamentalsProvider(Protocol):
    async def fundamentals(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]: ...


class NewsSearchProvider(Protocol):
    async def search(
        self,
        query: str,
        securities: Sequence[SecurityId],
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]: ...


class NotificationProvider(Protocol):
    async def deliver(
        self,
        *,
        recipient_id: str,
        template_id: str,
        payload: Mapping[str, JsonValue],
        context: ProviderRequestContext,
    ) -> str: ...
