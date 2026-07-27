"""可重复的美股 provider contract fake。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from trade_agent.capabilities.market_research.contracts import (
    SecurityId,
    SecurityResolution,
    SecurityResolutionStatus,
)
from trade_agent.capabilities.market_research.ports import (
    ProviderError,
    ProviderErrorCode,
    ProviderObservation,
    ProviderRequestContext,
)
from trade_agent.core.llm.contracts import JsonValue


class FakeProviderScenario(StrEnum):
    NORMAL = "normal"
    STALE = "stale"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FakeMarketProvider:
    securities: tuple[SecurityId, ...]
    observations: Mapping[str, tuple[ProviderObservation, ...]] = field(default_factory=dict)
    scenario: FakeProviderScenario = FakeProviderScenario.NORMAL

    async def resolve(self, query: str, context: ProviderRequestContext) -> SecurityResolution:
        del context
        self._raise_for_scenario()
        normalized = query.strip().upper()
        matches = tuple(
            item
            for item in self.securities
            if item.symbol.upper() == normalized or item.display_name.upper() == normalized
        )
        if len(matches) == 1:
            return SecurityResolution(SecurityResolutionStatus.RESOLVED, matches)
        if len(matches) > 1:
            return SecurityResolution(SecurityResolutionStatus.AMBIGUOUS, matches)
        return SecurityResolution(SecurityResolutionStatus.NOT_FOUND)

    async def quote(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> ProviderObservation:
        return (await self._read("quote", security, context))[0]

    async def klines(
        self,
        security: SecurityId,
        start: datetime,
        end: datetime,
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]:
        del start, end
        return await self._read("kline", security, context)

    async def corporate_actions(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]:
        return await self._read("corporate_action", security, context)

    async def fundamentals(
        self, security: SecurityId, context: ProviderRequestContext
    ) -> Sequence[ProviderObservation]:
        return await self._read("fundamental", security, context)

    async def search(
        self,
        query: str,
        securities: Sequence[SecurityId],
        context: ProviderRequestContext,
    ) -> Sequence[ProviderObservation]:
        del query, securities
        self._raise_for_scenario()
        return self.observations.get("news", ())

    async def deliver(
        self,
        *,
        recipient_id: str,
        template_id: str,
        payload: Mapping[str, JsonValue],
        context: ProviderRequestContext,
    ) -> str:
        del recipient_id, template_id, payload, context
        self._raise_for_scenario()
        return "fake-delivery-1"

    async def _read(
        self,
        evidence_type: str,
        security: SecurityId,
        context: ProviderRequestContext,
    ) -> tuple[ProviderObservation, ...]:
        del security, context
        self._raise_for_scenario()
        items = self.observations.get(evidence_type, ())
        if not items:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                f"fake 未配置 {evidence_type} observation",
                retryable=False,
            )
        if self.scenario is FakeProviderScenario.CONFLICT and len(items) < 2:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "conflict 场景必须配置至少两条 observation",
                retryable=False,
            )
        return items

    def _raise_for_scenario(self) -> None:
        if self.scenario is FakeProviderScenario.RATE_LIMITED:
            raise ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "fake rate limit",
                retryable=True,
                retry_after_seconds=1,
            )
        if self.scenario is FakeProviderScenario.TIMEOUT:
            raise ProviderError(ProviderErrorCode.TIMEOUT, "fake timeout", retryable=True)
        if self.scenario is FakeProviderScenario.UNAVAILABLE:
            raise ProviderError(ProviderErrorCode.UNAVAILABLE, "fake unavailable", retryable=True)
