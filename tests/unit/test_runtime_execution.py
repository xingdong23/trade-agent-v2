import asyncio
from pathlib import Path

import pytest

from trade_agent.adapters.sqlite import SQLiteCommandStore, SQLiteDatabase
from trade_agent.adapters.sqlite.json_support import payload_hash
from trade_agent.core.runtime import (
    NodeErrorCode,
    NodeExecutionError,
    NodeExecutionPolicy,
    NodeExecutor,
    execute_idempotent_command,
)


def test_node_executor_retries_bounded_provider_failures() -> None:
    attempts = 0

    class TemporaryError(RuntimeError):
        code = "unavailable"
        retryable = True

    async def operation() -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TemporaryError("down")
        return {"status": "ok"}

    result = asyncio.run(NodeExecutor(NodeExecutionPolicy(max_attempts=3)).run(operation))
    assert result == {"status": "ok"}
    assert attempts == 3


def test_node_executor_emits_terminal_timeout() -> None:
    async def operation() -> dict[str, str]:
        await asyncio.sleep(0.02)
        return {}

    with pytest.raises(NodeExecutionError) as failure:
        asyncio.run(
            NodeExecutor(NodeExecutionPolicy(timeout_seconds=0.001, max_attempts=2)).run(operation)
        )
    assert failure.value.code is NodeErrorCode.RETRY_EXHAUSTED
    assert failure.value.attempts == 2


def test_idempotent_command_reuses_committed_result_after_checkpoint_gap(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "commands.db")
    database.initialize()
    store = SQLiteCommandStore(database)
    calls = 0

    async def operation() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"plan_id": "plan-1"}

    async def run() -> None:
        digest = payload_hash({"symbol": "NVDA"})
        first = await execute_idempotent_command(
            store=store,
            owner_id="owner-a",
            idempotency_key="run-1:node-1",
            payload_hash=digest,
            operation=operation,
        )
        replay = await execute_idempotent_command(
            store=store,
            owner_id="owner-a",
            idempotency_key="run-1:node-1",
            payload_hash=digest,
            operation=operation,
        )
        assert first == replay == {"plan_id": "plan-1"}

    asyncio.run(run())
    assert calls == 1
