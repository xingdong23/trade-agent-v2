"""Watchlist 与 Strategy 薄 tool adapter 契约测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from trade_agent.capabilities.strategy.application import StrategyPublishingService
from trade_agent.capabilities.strategy.contracts import StrategyDraft, StrategyPublisher
from trade_agent.capabilities.strategy.tools import PublishStrategyTool
from trade_agent.capabilities.watchlist.application import (
    IdempotencyConflictError,
    WatchlistService,
)
from trade_agent.capabilities.watchlist.contracts import ImportStatus
from trade_agent.capabilities.watchlist.tools import (
    AcceptClassificationSuggestionTool,
    ApproveWatchlistImportTool,
    FreezeUniverseTool,
    ValidateWatchlistImportTool,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest


def _trusted_context() -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-a"))


def test_watchlist_tool_manifests_declare_schema_and_safety_metadata() -> None:
    service = WatchlistService(owner_id="owner-a", watchlist_id="watchlist-a")
    tools = (
        ValidateWatchlistImportTool(service),
        ApproveWatchlistImportTool(service),
        AcceptClassificationSuggestionTool(service),
        FreezeUniverseTool(service),
    )

    for tool in tools:
        assert tool.manifest.input_schema["type"] == "object"
        assert tool.manifest.output_schema["type"] == "object"
        assert tool.manifest.side_effect
        assert tool.manifest.risk
        assert isinstance(tool.manifest.requires_hitl, bool)
        assert isinstance(tool.manifest.idempotent, bool)
        assert isinstance(tool.manifest.requires_idempotency_key, bool)
    assert tools[0].manifest.read_only is True
    assert tools[0].manifest.side_effect == "none"
    assert tools[1].manifest.requires_hitl is True
    assert tools[1].manifest.idempotent is True
    assert tools[1].manifest.requires_idempotency_key is True
    assert tools[2].manifest.requires_hitl is True
    assert tools[2].manifest.requires_idempotency_key is True
    assert tools[3].manifest.requires_idempotency_key is False


def test_watchlist_import_tools_delegate_to_application_service() -> None:
    service = WatchlistService(owner_id="owner-a", watchlist_id="watchlist-a")
    validate = ValidateWatchlistImportTool(service)
    approved = ApproveWatchlistImportTool(service)
    rows: list[JsonValue] = [
        {
            "raw_value": "NVDA",
            "security_id": "US:NASDAQ:NVDA",
            "status": "accepted",
            "metadata": {"source": "user", "confidence": 0.99},
        },
        {"raw_value": "7203", "security_id": None, "status": "unsupported_market"},
    ]

    validation = asyncio.run(
        validate.handle(ToolRequest("watchlist.validate_import", {"rows": rows}))
    )
    assert validation.status == "validated"
    validated_rows = validation.payload["rows"]
    assert isinstance(validated_rows, list)
    first_row = validated_rows[0]
    assert isinstance(first_row, dict)
    assert first_row["status"] == ImportStatus.ACCEPTED.value
    assert first_row["metadata"] == {"source": "user", "confidence": 0.99}

    result = asyncio.run(
        approved.handle(
            ToolRequest(
                "watchlist.approve_import",
                {
                    "rows": validated_rows,
                    "approved": True,
                    "source_type": "manual",
                    "source_reference": "chat-1",
                    "imported_at": "2026-07-27T08:00:00Z",
                    "tags": {"US:NASDAQ:NVDA": ["semiconductor", "ai"]},
                    "notes": {"US:NASDAQ:NVDA": ["用户关注", "等待财报"]},
                },
                "import-command-1",
                context=_trusted_context(),
            )
        )
    )
    assert result.status == "imported"
    membership = service.memberships[0]
    assert membership.security_id == "US:NASDAQ:NVDA"
    assert membership.tags == frozenset({"semiconductor", "ai"})
    assert membership.notes == ("用户关注", "等待财报")
    imported_rows = result.payload["rows"]
    assert isinstance(imported_rows, list)
    imported_first = imported_rows[0]
    assert isinstance(imported_first, dict)
    assert imported_first["metadata"] == {"source": "user", "confidence": 0.99}


def test_watchlist_controlled_writes_require_hitl_idempotency_metadata() -> None:
    service = WatchlistService(owner_id="owner-a", watchlist_id="watchlist-a")
    approve = ApproveWatchlistImportTool(service)
    with pytest.raises(ValueError, match="idempotency key"):
        asyncio.run(
            approve.handle(
                ToolRequest(
                    "watchlist.approve_import",
                    {
                        "rows": [
                            {
                                "raw_value": "NVDA",
                                "security_id": "US:NASDAQ:NVDA",
                                "status": "accepted",
                            }
                        ],
                        "approved": True,
                        "source_type": "manual",
                        "source_reference": "chat-1",
                        "imported_at": "2026-07-27T08:00:00Z",
                    },
                    context=_trusted_context(),
                )
            )
        )


def test_classification_and_freeze_tools_only_project_application_results() -> None:
    service = WatchlistService(owner_id="owner-a", watchlist_id="watchlist-a")
    imported = service.classify_import((("NVDA", "US:NASDAQ:NVDA", ImportStatus.ACCEPTED),))
    service.approve_import(
        imported,
        actor_owner_id="owner-a",
        approved=True,
        idempotency_key="seed",
        source_type="manual",
        source_reference="chat-1",
        imported_at=datetime(2026, 7, 27, tzinfo=UTC),
    )
    service.create_group(actor_owner_id="owner-a", group_id="ai", name="AI 建议")
    service.create_group(actor_owner_id="owner-a", group_id="manual", name="用户修改")
    accept = AcceptClassificationSuggestionTool(service)
    arguments: dict[str, JsonValue] = {
        "suggestion_id": "suggestion-1",
        "security_id": "US:NASDAQ:NVDA",
        "proposed_group_id": "ai",
        "accepted_group_id": "manual",
        "source_reference": "model-output-1",
    }
    request = ToolRequest(
        "watchlist.accept_classification",
        arguments,
        "accept-command-1",
        context=_trusted_context(),
    )
    accepted = asyncio.run(accept.handle(request))
    replay = asyncio.run(accept.handle(request))
    assert replay == accepted
    with pytest.raises(IdempotencyConflictError, match="payload 已改变"):
        asyncio.run(
            accept.handle(
                ToolRequest(
                    "watchlist.accept_classification",
                    {**arguments, "accepted_group_id": "ai"},
                    "accept-command-1",
                    context=_trusted_context(),
                )
            )
        )
    assert accepted.payload["accepted"] is True
    assert accepted.payload["group_id"] == "manual"
    stored = service.get_suggestion(actor_owner_id="owner-a", suggestion_id="suggestion-1")
    assert stored.accepted_group_id == "manual"

    frozen = asyncio.run(
        FreezeUniverseTool(service).handle(
            ToolRequest(
                "watchlist.freeze_universe",
                {
                    "created_at": "2026-07-27T08:30:00Z",
                    "group_id": "manual",
                },
                context=_trusted_context(),
            )
        )
    )
    assert frozen.payload["security_ids"] == ["US:NASDAQ:NVDA"]


def test_strategy_publish_manifest_and_handler_remain_thin() -> None:
    draft = StrategyDraft(
        "strategy-1",
        "owner-a",
        "趋势",
        "趋势确认",
        "direction",
        "5d",
        ({"feature": "trend_20d", "operator": ">", "value": 0},),
        (),
        ("trend_20d",),
        {"function": "score.v1"},
    )
    tool = PublishStrategyTool(StrategyPublishingService(StrategyPublisher()), draft)
    assert tool.manifest.input_schema["type"] == "object"
    assert tool.manifest.output_schema["type"] == "object"
    assert tool.manifest.side_effect == "create_strategy_version"
    assert tool.manifest.risk == "controlled_write"
    assert tool.manifest.requires_hitl is True
    assert tool.manifest.idempotent is True
    assert tool.manifest.requires_idempotency_key is True
    properties = tool.manifest.input_schema["properties"]
    assert isinstance(properties, dict)
    assert "strategy_id" in properties

    result = asyncio.run(
        tool.handle(
            ToolRequest(
                "strategy.publish",
                {
                    "strategy_id": draft.strategy_id,
                    "approved": True,
                    "payload_hash": draft.content_hash,
                },
                "publish-command-1",
                context=_trusted_context(),
            )
        )
    )
    assert result.status == "published"
    assert result.payload == {"strategy_id": "strategy-1", "version": 1}


def test_strategy_publish_rejects_wrong_subject_or_stale_payload_hash() -> None:
    draft = StrategyDraft(
        "strategy-1",
        "owner-a",
        "趋势",
        "趋势确认",
        "direction",
        "5d",
        ({"feature": "trend_20d", "operator": ">", "value": 0},),
        (),
        ("trend_20d",),
        {"function": "score.v1"},
    )
    tool = PublishStrategyTool(StrategyPublishingService(StrategyPublisher()), draft)
    common: dict[str, JsonValue] = {
        "approved": True,
        "payload_hash": draft.content_hash,
    }
    with pytest.raises(ValueError, match="strategy_id"):
        asyncio.run(
            tool.handle(
                ToolRequest(
                    "strategy.publish",
                    {**common, "strategy_id": "strategy-other"},
                    "publish-wrong-subject",
                    context=_trusted_context(),
                )
            )
        )
    with pytest.raises(ValueError, match="payload_hash"):
        asyncio.run(
            tool.handle(
                ToolRequest(
                    "strategy.publish",
                    {**common, "strategy_id": draft.strategy_id, "payload_hash": "stale"},
                    "publish-stale-hash",
                    context=_trusted_context(),
                )
            )
        )
