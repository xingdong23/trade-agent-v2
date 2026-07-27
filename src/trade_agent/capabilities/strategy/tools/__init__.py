"""只调用 strategy application service 的薄 tool adapter。"""

from dataclasses import dataclass

from trade_agent.capabilities.strategy.application import StrategyPublishingService
from trade_agent.capabilities.strategy.contracts import StrategyDraft
from trade_agent.core.tools import ToolManifest, ToolRequest, ToolResult
from trade_agent.core.tools.identity import bind_trusted_identity, identity_fields_for_manifest


@dataclass(frozen=True, slots=True)
class PublishStrategyTool:
    application: StrategyPublishingService
    draft: StrategyDraft

    manifest = ToolManifest(
        tool_id="strategy.publish",
        description="发布经过用户批准的策略版本",
        read_only=False,
        requires_hitl=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["strategy_id", "approved", "actor_id", "payload_hash"],
            "properties": {
                "strategy_id": {"type": "string", "minLength": 1},
                "approved": {"const": True},
                "actor_id": {"type": "string", "minLength": 1},
                "payload_hash": {"type": "string", "minLength": 1},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["strategy_id", "version"],
            "properties": {
                "strategy_id": {"type": "string"},
                "version": {"type": "integer", "minimum": 1},
            },
        },
        side_effect="create_strategy_version",
        risk="controlled_write",
        idempotent=True,
        requires_idempotency_key=True,
    )

    async def handle(self, request: ToolRequest) -> ToolResult:
        request = bind_trusted_identity(
            request,
            identity_fields=identity_fields_for_manifest(self.manifest),
        )
        if request.tool_id != self.manifest.tool_id:
            raise ValueError("tool id 与 handler 不匹配")
        if request.idempotency_key is None or not request.idempotency_key.strip():
            raise ValueError("策略发布必须提供 idempotency key")
        approved = request.arguments.get("approved")
        strategy_id = request.arguments.get("strategy_id")
        actor_id = request.arguments.get("actor_id")
        payload_hash = request.arguments.get("payload_hash")
        if (
            not isinstance(approved, bool)
            or not isinstance(strategy_id, str)
            or not isinstance(actor_id, str)
            or not isinstance(payload_hash, str)
            or not strategy_id.strip()
            or not actor_id.strip()
            or not payload_hash.strip()
        ):
            raise ValueError("策略发布参数不符合 schema")
        if strategy_id != self.draft.strategy_id:
            raise ValueError("strategy_id 与待发布草稿不匹配")
        if payload_hash != self.draft.content_hash:
            raise ValueError("payload_hash 与待发布草稿内容不匹配")
        result = self.application.publish(
            self.draft,
            actor_id=actor_id,
            approved=approved,
            payload_hash=payload_hash,
            idempotency_key=request.idempotency_key,
        )
        return ToolResult(
            "published", {"strategy_id": result.strategy_id, "version": result.version}
        )


__all__ = ["PublishStrategyTool"]
