"""所有市场 provider adapter 可复用的首版契约测试。"""

import asyncio
from datetime import UTC, datetime

import pytest

from trade_agent.adapters.market_providers import FakeMarketProvider, FakeProviderScenario
from trade_agent.capabilities.market_research.contracts import Market, SecurityId
from trade_agent.capabilities.market_research.ports import (
    ProviderError,
    ProviderErrorCode,
    ProviderObservation,
    ProviderRequestContext,
)


def _fixture(
    scenario: FakeProviderScenario = FakeProviderScenario.NORMAL,
) -> FakeMarketProvider:
    now = datetime.now(UTC)
    security = SecurityId(Market.US, "NASDAQ", "NVDA", "NVIDIA Corporation")
    quote = ProviderObservation(
        provider="fake",
        evidence_type="quote",
        source_reference="fake:quote:NVDA",
        observed_at=now,
        published_at=None,
        retrieved_at=now,
        payload={"price": 120.0},
        entitlement={"scope": "test"},
    )
    return FakeMarketProvider((security,), {"quote": (quote,)}, scenario)


def _context() -> ProviderRequestContext:
    return ProviderRequestContext("correlation-1", datetime.now(UTC), "test")


def test_fake_provider_returns_deterministic_typed_observation() -> None:
    provider = _fixture()
    security = provider.securities[0]

    first = asyncio.run(provider.quote(security, _context()))
    second = asyncio.run(provider.quote(security, _context()))

    assert first == second
    assert first.source_reference == "fake:quote:NVDA"
    assert first.entitlement == {"scope": "test"}


def test_fake_provider_exposes_ambiguous_resolution_without_guessing() -> None:
    provider = _fixture()
    duplicate = SecurityId(Market.US, "NYSE", "NVDA", "NVIDIA Depositary")
    provider = FakeMarketProvider((*provider.securities, duplicate), provider.observations)

    result = asyncio.run(provider.resolve("NVDA", _context()))

    assert result.status.value == "ambiguous"
    assert len(result.candidates) == 2


def test_fake_provider_preserves_stale_timestamp_and_conflicting_values() -> None:
    provider = _fixture()
    original = provider.observations["quote"][0]
    stale = ProviderObservation(
        provider="fake-a",
        evidence_type="fundamental",
        source_reference="fake-a:fundamental:NVDA",
        observed_at=original.observed_at,
        published_at=None,
        retrieved_at=original.retrieved_at,
        payload={"revenue": 100},
        entitlement={"scope": "test"},
    )
    conflict = ProviderObservation(
        provider="fake-b",
        evidence_type="fundamental",
        source_reference="fake-b:fundamental:NVDA",
        observed_at=original.observed_at,
        published_at=None,
        retrieved_at=original.retrieved_at,
        payload={"revenue": 101},
        entitlement={"scope": "test"},
    )
    provider = FakeMarketProvider(
        provider.securities,
        {"fundamental": (stale, conflict)},
        FakeProviderScenario.CONFLICT,
    )

    result = asyncio.run(provider.fundamentals(provider.securities[0], _context()))

    assert [item.payload["revenue"] for item in result] == [100, 101]
    assert result[0].observed_at == stale.observed_at


@pytest.mark.parametrize(
    ("scenario", "code", "retry_after"),
    [
        (FakeProviderScenario.RATE_LIMITED, ProviderErrorCode.RATE_LIMITED, 1),
        (FakeProviderScenario.TIMEOUT, ProviderErrorCode.TIMEOUT, None),
        (FakeProviderScenario.UNAVAILABLE, ProviderErrorCode.UNAVAILABLE, None),
    ],
)
def test_fake_provider_normalizes_retryable_failures(
    scenario: FakeProviderScenario,
    code: ProviderErrorCode,
    retry_after: float | None,
) -> None:
    provider = _fixture(scenario)

    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.quote(provider.securities[0], _context()))

    assert error.value.code is code
    assert error.value.retryable is True
    assert error.value.retry_after_seconds == retry_after


def test_fake_provider_rejects_unconfigured_data_instead_of_inventing_it() -> None:
    provider = FakeMarketProvider((_fixture().securities[0],))

    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.quote(provider.securities[0], _context()))

    assert error.value.code is ProviderErrorCode.INVALID_RESPONSE
    assert error.value.retryable is False
