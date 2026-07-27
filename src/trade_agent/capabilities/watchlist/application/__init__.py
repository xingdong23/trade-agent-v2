from trade_agent.capabilities.watchlist.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
)

from .service import IdempotencyConflictError, WatchlistService


class WatchlistApplication:
    """Phase-one public application boundary."""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"watchlist command 尚未实现: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"watchlist query 尚未实现: {query.query_id}")


__all__ = ["IdempotencyConflictError", "WatchlistApplication", "WatchlistService"]
