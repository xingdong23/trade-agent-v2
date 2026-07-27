"""Strategy capability 的草稿与发布用例。"""

from trade_agent.capabilities.strategy.contracts import (
    CapabilityCommand,
    CapabilityQuery,
    CapabilityResult,
    PublishedStrategy,
    StrategyDraft,
    StrategyPublisher,
)


class StrategyApplication:
    """Phase-one public application boundary."""

    async def execute(self, command: CapabilityCommand) -> CapabilityResult:
        raise NotImplementedError(f"strategy command 尚未实现: {command.command_id}")

    async def query(self, query: CapabilityQuery) -> CapabilityResult:
        raise NotImplementedError(f"strategy query 尚未实现: {query.query_id}")


class StrategyPublishingService:
    def __init__(self, publisher: StrategyPublisher) -> None:
        self._publisher = publisher

    def publish(
        self,
        draft: StrategyDraft,
        *,
        actor_id: str,
        approved: bool,
        payload_hash: str,
        idempotency_key: str,
    ) -> PublishedStrategy:
        return self._publisher.publish(
            draft,
            actor_id=actor_id,
            approved=approved,
            source_draft_hash=payload_hash,
            idempotency_key=idempotency_key,
        )


__all__ = ["StrategyApplication", "StrategyPublishingService"]
