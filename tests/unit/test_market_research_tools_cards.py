"""市场研究薄 tool 与确定性 Card presenter 测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from trade_agent.capabilities.contracts import CapabilityResult
from trade_agent.capabilities.market_research.cards import MarketResearchCardPresenter
from trade_agent.capabilities.market_research.tools import (
    ResearchSecurityTool,
    ResearchThemeTool,
    ResolveSecurityTool,
)
from trade_agent.core.llm.contracts import JsonValue
from trade_agent.core.presentation import DEFAULT_CARD_CATALOG, CardEnvelope
from trade_agent.core.tools import ToolExecutionContext, ToolExecutionPrincipal, ToolRequest


class FakeResearchApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, JsonValue]]] = []

    async def resolve_security(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        self.calls.append(("resolve", arguments))
        return {"status": "resolved", "security_id": "US:NASDAQ:NVDA"}

    async def research_security(
        self, arguments: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        self.calls.append(("security", arguments))
        return {"artifact_id": "research-1"}

    async def research_theme(self, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        self.calls.append(("theme", arguments))
        return {"artifact_id": "theme-1", "watchlist_proposal_only": True}


def _trusted_context() -> ToolExecutionContext:
    return ToolExecutionContext(ToolExecutionPrincipal(owner_id="owner-a"))


def test_market_research_tools_only_delegate_to_application() -> None:
    app = FakeResearchApplication()
    resolved = asyncio.run(
        ResolveSecurityTool(app).handle(
            ToolRequest(
                "market_research.resolve_security",
                {"query": "NVDA"},
                context=_trusted_context(),
            )
        )
    )
    researched = asyncio.run(
        ResearchSecurityTool(app).handle(
            ToolRequest(
                "market_research.research_security",
                {
                    "security_id": "US:NASDAQ:NVDA",
                    "as_of": "2026-07-27T09:30:00Z",
                },
                context=_trusted_context(),
            )
        )
    )
    theme = asyncio.run(
        ResearchThemeTool(app).handle(
            ToolRequest(
                "market_research.research_theme",
                {"theme": "AI 算力", "as_of": "2026-07-27T09:30:00Z"},
                context=_trusted_context(),
            )
        )
    )
    assert resolved.payload["status"] == "resolved"
    assert researched.payload["artifact_id"] == "research-1"
    assert theme.payload["watchlist_proposal_only"] is True
    assert [call[0] for call in app.calls] == ["resolve", "security", "theme"]
    assert all(arguments["owner_id"] == "owner-a" for _, arguments in app.calls)


def _data(card: CardEnvelope) -> dict[str, Any]:
    return cast(dict[str, Any], card.data)


def test_research_cards_preserve_citations_freshness_and_gaps() -> None:
    presenter = MarketResearchCardPresenter()
    evidence: list[JsonValue] = [
        {
            "evidence_id": "e-1",
            "provider": "provider-a",
            "source_reference": "quote:NVDA",
            "freshness": "fresh",
            "retrieved_at": "2026-07-27T09:30:00Z",
        }
    ]
    artifact = presenter.present(
        CapabilityResult(
            "research-1",
            1,
            {
                "card_type": "research_artifact",
                "title": "NVDA 研究",
                "summary": "基于已接受 evidence 的结构化研究",
                "sections": [{"title": "风险", "content": "竞争加剧 [e-1]", "kind": "risk"}],
                "evidence": evidence,
            },
        )
    )
    gap = presenter.present(
        CapabilityResult(
            "research-1",
            2,
            {
                "card_type": "data_gap",
                "title": "数据缺口",
                "message": "基本面 provider 不可用",
                "missing_fields": ["fundamentals"],
                "evidence": evidence,
            },
        )
    )
    failure = presenter.present(
        CapabilityResult(
            "job-1",
            1,
            {
                "card_type": "failure",
                "title": "研究失败",
                "message": "provider timeout",
                "error_code": "timeout",
                "retryable": True,
            },
        )
    )
    assert artifact.kind == "artifact.research"
    assert "freshness=fresh" in str(_data(artifact)["provenance"])
    assert gap.kind == "notice.data_gap"
    assert _data(gap)["missing_fields"] == ["fundamentals"]
    assert failure.kind == "notice.failure"
    assert failure.actions == ("retry", "cancel")
    for card in (artifact, gap, failure):
        assert DEFAULT_CARD_CATALOG.supports(card.kind, card.schema_version)
