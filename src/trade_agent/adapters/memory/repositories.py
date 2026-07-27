"""与 SQLite 版本仓储保持相同契约的单元测试 fake。"""

from collections.abc import Mapping

from trade_agent.capabilities.contracts import CapabilityResult, ConcurrentWriteError
from trade_agent.core.llm.contracts import JsonValue


class InMemoryAggregateRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[CapabilityResult]] = {}

    def save(
        self,
        *,
        owner_id: str,
        aggregate_id: str,
        expected_version: int,
        payload: Mapping[str, JsonValue],
        schema_version: int = 1,
    ) -> CapabilityResult:
        del schema_version
        key = (owner_id, aggregate_id)
        versions = self._records.setdefault(key, [])
        if len(versions) != expected_version:
            raise ConcurrentWriteError(
                f"版本冲突: expected={expected_version}, actual={len(versions)}"
            )
        result = CapabilityResult(aggregate_id, expected_version + 1, dict(payload))
        versions.append(result)
        return result

    def get(self, owner_id: str, aggregate_id: str) -> CapabilityResult | None:
        versions = self._records.get((owner_id, aggregate_id), [])
        return versions[-1] if versions else None
