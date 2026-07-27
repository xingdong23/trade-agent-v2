"""不可变策略发布、tool metadata 与卡片投影测试。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from trade_agent.capabilities.strategy.application import StrategyPublishingService
from trade_agent.capabilities.strategy.cards import StrategyCardPresenter
from trade_agent.capabilities.strategy.contracts import StrategyDraft, StrategyPublisher
from trade_agent.capabilities.strategy.tools import PublishStrategyTool
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest


def _draft(name: str = "趋势策略") -> StrategyDraft:
    return StrategyDraft(
        "strategy-1",
        "owner-a",
        name,
        "价格趋势与流动性共同确认",
        "direction",
        "5d",
        ({"feature": "trend_20d", "operator": ">", "value": 0},),
        ({"feature": "dollar_volume_20d", "operator": "<", "value": 1_000_000},),
        ("trend_20d", "dollar_volume_20d"),
        {"function": "score.v1"},
        ("NVDA",),
        ("ILLIQUID",),
    )


def _trusted_context() -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-a"))


def test_publish_requires_approval_and_preserves_old_versions() -> None:
    publisher = StrategyPublisher()
    first_draft = _draft()
    second_draft = _draft("趋势策略新版")
    with pytest.raises(PermissionError):
        publisher.publish(
            first_draft,
            actor_id="owner-a",
            approved=False,
            source_draft_hash=first_draft.content_hash,
            idempotency_key="publish-1",
        )
    first = publisher.publish(
        first_draft,
        actor_id="owner-a",
        approved=True,
        source_draft_hash=first_draft.content_hash,
        idempotency_key="publish-1",
    )
    second = publisher.publish(
        second_draft,
        actor_id="owner-a",
        approved=True,
        source_draft_hash=second_draft.content_hash,
        idempotency_key="publish-2",
    )
    assert first.version == 1 and second.version == 2
    assert publisher.get_version("owner-a", "strategy-1", 1).draft.name == "趋势策略"
    with pytest.raises(LookupError):
        publisher.get_version("owner-b", "strategy-1", 1)
    with pytest.raises(LookupError):
        publisher.get_version("owner-a", "strategy-1", 0)


def test_draft_copies_nested_mappings_before_publishing() -> None:
    condition: dict[str, JsonValue] = {"feature": "trend_20d", "operator": ">", "value": 0}
    ranking: dict[str, JsonValue] = {"function": "score.v1"}
    draft = StrategyDraft(
        "strategy-immutable",
        "owner-a",
        "不可变策略",
        "趋势确认",
        "direction",
        "5d",
        (condition,),
        (),
        ("trend_20d",),
        ranking,
    )
    condition["value"] = -1
    ranking["function"] = "changed"
    assert draft.entry_conditions[0]["value"] == 0
    assert draft.ranking_policy["function"] == "score.v1"


def test_publish_is_owner_scoped_idempotent_and_concurrency_safe() -> None:
    publisher = StrategyPublisher()
    draft = _draft()
    with pytest.raises(PermissionError):
        publisher.publish(
            draft,
            actor_id="owner-b",
            approved=True,
            source_draft_hash=draft.content_hash,
            idempotency_key="publish-1",
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _: publisher.publish(
                    draft,
                    actor_id="owner-a",
                    approved=True,
                    source_draft_hash=draft.content_hash,
                    idempotency_key="publish-1",
                ),
                range(2),
            )
        )
    assert results[0] is results[1]
    assert results[0].version == 1
    with pytest.raises(RuntimeError):
        changed = _draft("被篡改的草稿")
        publisher.publish(
            changed,
            actor_id="owner-a",
            approved=True,
            source_draft_hash=changed.content_hash,
            idempotency_key="publish-1",
        )


def test_publish_rejects_claimed_hash_that_does_not_match_draft() -> None:
    with pytest.raises(ValueError, match="内容不一致"):
        StrategyPublisher().publish(
            _draft(),
            actor_id="owner-a",
            approved=True,
            source_draft_hash="0" * 64,
            idempotency_key="publish-1",
        )


def test_publish_tool_is_thin_hitl_and_idempotency_declared() -> None:
    draft = _draft()
    tool = PublishStrategyTool(StrategyPublishingService(StrategyPublisher()), draft)
    assert tool.manifest.requires_hitl
    assert tool.manifest.idempotent
    assert tool.manifest.requires_idempotency_key
    assert tool.manifest.side_effect == "create_strategy_version"
    result = asyncio.run(
        tool.handle(
            ToolRequest(
                "strategy.publish",
                {
                    "strategy_id": draft.strategy_id,
                    "approved": True,
                    "payload_hash": draft.content_hash,
                },
                "command-1",
                context=_trusted_context(),
            )
        )
    )
    assert result.payload["version"] == 1


def test_strategy_cards_use_catalog_allowlist_and_expose_review_before_approval() -> None:
    presenter = StrategyCardPresenter()
    review = presenter.review(_draft())
    approval = presenter.approval(_draft(), revision=2)
    assert review.kind == "interaction.review"
    assert approval.kind == "interaction.approval"
    assert approval.actions == ("confirm", "edit", "cancel")
