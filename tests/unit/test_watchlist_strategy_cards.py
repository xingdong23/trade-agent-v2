from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from trade_agent.capabilities.strategy.cards import StrategyCardPresenter
from trade_agent.capabilities.strategy.contracts import PublishedStrategy, StrategyDraft
from trade_agent.capabilities.watchlist.cards import WatchlistCardPresenter
from trade_agent.capabilities.watchlist.contracts import (
    ClassificationSuggestion,
    ImportRow,
    ImportStatus,
    Membership,
    Provenance,
    Watchlist,
    WatchlistGroup,
)
from trade_agent.core.presentation import DEFAULT_CARD_CATALOG, CardEnvelope


def _data(card: CardEnvelope) -> dict[str, Any]:
    return cast(dict[str, Any], card.data)


def test_watchlist_import_cards_cover_form_review_and_approval() -> None:
    presenter = WatchlistCardPresenter()
    watchlist = Watchlist("wl-1", "owner-a", "美股观察", 3)
    group = WatchlistGroup("g-semiconductor", "半导体", frozenset({"NVDA"}))
    rows = (
        ImportRow(1, "AAPL", ImportStatus.ACCEPTED, "us:AAPL"),
        ImportRow(2, "NVDA", ImportStatus.DUPLICATE, "us:NVDA"),
        ImportRow(3, "ABC", ImportStatus.AMBIGUOUS, None, "存在多个候选"),
        ImportRow(4, "00700.HK", ImportStatus.UNSUPPORTED_MARKET, None, "仅支持美股"),
        ImportRow(5, "???", ImportStatus.REJECTED, None, "无法识别"),
    )

    form = presenter.import_form(
        watchlist,
        draft_symbols="AAPL\nNVDA\nABC",
        source_type="research_summary",
        source_reference="对话线程 thread-1",
        groups=(group,),
    )
    review = presenter.import_review(
        watchlist,
        rows,
        source_type="research_summary",
        source_reference="对话线程 thread-1",
        target_group=group,
    )
    approval = presenter.import_approval(
        watchlist,
        rows,
        source_type="research_summary",
        source_reference="对话线程 thread-1",
        target_group=group,
        imported_at=datetime(2026, 7, 27, 9, 30, 0),
    )

    assert form.kind == "interaction.form"
    assert form.actions == ("continue", "cancel")
    form_data = _data(form)
    review_data = _data(review)
    approval_data = _data(approval)
    assert form_data["fields"][0]["key"] == "symbols_text"
    assert form_data["fields"][3]["options"][0]["key"] == group.group_id
    assert review.kind == "interaction.review"
    assert any(finding["label"] == "第 4 行 00700.HK" for finding in review_data["findings"])
    assert any("accepted 1" in finding["detail"] for finding in review_data["findings"])
    assert approval.kind == "interaction.approval"
    assert approval_data["summary"].endswith("目标版本 v4。")
    assert any(
        fact["label"] == "目标版本" and fact["detail"] == "v4" for fact in approval_data["facts"]
    )
    assert any(
        fact["label"] == "阻塞项" and "00700.HK" in fact["detail"]
        for fact in approval_data["facts"]
    )
    assert DEFAULT_CARD_CATALOG.supports(approval.kind, approval.schema_version)


def test_watchlist_suggestion_cards_keep_review_before_approval_without_mutation() -> None:
    presenter = WatchlistCardPresenter()
    watchlist = Watchlist("wl-1", "owner-a", "美股观察", 7)
    current_group = WatchlistGroup("g-core", "核心观察", frozenset({"us:MSFT"}))
    proposed_group = WatchlistGroup("g-cloud", "云计算", frozenset())
    membership = Membership(
        "us:MSFT",
        frozenset({"quality"}),
        ("长期关注",),
        (Provenance("manual_input", "用户输入", datetime(2026, 7, 26, 9, 0, 0)),),
    )
    suggestion = ClassificationSuggestion("sg-1", "us:MSFT", proposed_group.group_id, "ai-theme")

    review = presenter.suggestion_review(
        watchlist,
        suggestion,
        membership=membership,
        proposed_group=proposed_group,
        current_groups=(current_group,),
    )
    approval = presenter.suggestion_approval(
        watchlist,
        suggestion,
        membership=membership,
        proposed_group=proposed_group,
        current_groups=(current_group,),
    )

    assert not suggestion.accepted
    assert review.kind == "interaction.review"
    review_data = _data(review)
    approval_data = _data(approval)
    assert any(
        finding["label"] == "当前分组" and "核心观察" in finding["detail"]
        for finding in review_data["findings"]
    )
    assert approval.kind == "interaction.approval"
    assert approval.actions == ("confirm", "edit", "cancel")
    assert any(
        fact["label"] == "分组差异" and "云计算" in fact["detail"]
        for fact in approval_data["facts"]
    )


def test_strategy_cards_show_target_version_diff_and_strategy_artifact_kind() -> None:
    presenter = StrategyCardPresenter()
    previous_draft = StrategyDraft(
        "strategy-1",
        "owner-a",
        "趋势策略",
        "价格趋势与流动性确认",
        "direction",
        "5d",
        ({"feature": "trend_20d", "operator": ">", "value": 0},),
        (),
        ("trend_20d", "dollar_volume_20d"),
        {"function": "score.v1"},
    )
    previous = PublishedStrategy("strategy-1", "owner-a", 1, previous_draft, "owner-a", "hash-v1")
    draft = StrategyDraft(
        "strategy-1",
        "owner-a",
        "趋势策略增强版",
        "价格趋势、盈利修正与流动性共同确认",
        "direction",
        "10d",
        (
            {"feature": "trend_20d", "operator": ">", "value": 0},
            {"feature": "eps_revision_30d", "operator": ">", "value": 0},
        ),
        ({"feature": "halt_flag", "operator": "==", "value": 1},),
        ("trend_20d", "dollar_volume_20d", "eps_revision_30d"),
        {"function": "score.v2", "weight": "quality_bias"},
        ("NVDA",),
        ("GME",),
    )
    published = PublishedStrategy("strategy-1", "owner-a", 2, draft, "owner-a", "hash-v2")

    review = presenter.review(draft, previous=previous)
    approval = presenter.approval(draft, previous=previous, revision=2)
    artifact = presenter.artifact(published)

    assert review.kind == "interaction.review"
    review_data = _data(review)
    approval_data = _data(approval)
    artifact_data = _data(artifact)
    assert any(
        finding["label"] == "目标版本" and finding["detail"] == "v2"
        for finding in review_data["findings"]
    )
    assert any(finding["label"] == "名称变化" for finding in review_data["findings"])
    assert approval.kind == "interaction.approval"
    assert any(fact["label"] == "排序规则变化" for fact in approval_data["facts"])
    assert artifact.kind == "artifact.strategy"
    assert artifact.state == "resolved"
    assert artifact_data["sections"][0]["title"] == "策略逻辑"
