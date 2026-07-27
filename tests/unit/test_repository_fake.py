"""版本仓储 fake 的 owner scope 和并发契约。"""

import pytest

from trade_agent.adapters.memory import InMemoryAggregateRepository
from trade_agent.adapters.sqlite import ConcurrentWriteError
from trade_agent.capabilities.contracts import CapabilityRepository


def test_fake_implements_repository_contract_and_owner_scope() -> None:
    repository: CapabilityRepository = InMemoryAggregateRepository()
    saved = repository.save(
        owner_id="owner-a",
        aggregate_id="watchlist-1",
        expected_version=0,
        payload={"name": "关注列表"},
    )

    assert repository.get("owner-a", "watchlist-1") == saved
    assert repository.get("owner-b", "watchlist-1") is None
    with pytest.raises(ConcurrentWriteError):
        repository.save(
            owner_id="owner-a",
            aggregate_id="watchlist-1",
            expected_version=0,
            payload={"name": "过期写入"},
        )
