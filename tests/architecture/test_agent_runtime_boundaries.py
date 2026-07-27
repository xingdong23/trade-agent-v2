"""业务 Agent 与 quantitative capability 的结构性隔离。"""

import ast
import asyncio
from pathlib import Path

from trade_agent.agents.planning.subgraph import build_subgraph as build_planning
from trade_agent.agents.research import MANIFEST as RESEARCH_MANIFEST
from trade_agent.agents.research.subgraph import build_subgraph as build_research
from trade_agent.agents.strategy.subgraph import build_subgraph as build_strategy
from trade_agent.agents.supervisor import BUSINESS_AGENTS
from trade_agent.agents.supervisor import MANIFEST as SUPERVISOR_MANIFEST
from trade_agent.core.testing import FakeToolGateway
from trade_agent.core.tools import ToolRequest

PACKAGE_ROOT = Path(__file__).parents[2] / "src" / "trade_agent"


def test_agents_do_not_import_capability_provider_or_repository() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "agents").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if module and module.startswith(
                ("trade_agent.capabilities", "trade_agent.adapters", "trade_agent.apps")
            ):
                violations.append(f"{path.name}: {module}")
    assert not violations


def test_only_three_business_agents_exist_and_quantitative_is_tool_only() -> None:
    assert tuple(manifest.agent_id for manifest in BUSINESS_AGENTS) == (
        "research",
        "strategy",
        "planning",
    )
    assert SUPERVISOR_MANIFEST.allowed_tool_ids == ()
    assert not (PACKAGE_ROOT / "agents" / "quantitative").exists()
    assert {
        "quantitative.get_prediction",
        "quantitative.get_quantitative_snapshot",
    } <= set(RESEARCH_MANIFEST.allowed_tool_ids)


def test_each_business_agent_has_versioned_prompt_and_compiled_subgraph() -> None:
    gateway = FakeToolGateway()
    subgraphs = (build_research(gateway), build_strategy(gateway), build_planning(gateway))
    for subgraph in subgraphs:
        assert subgraph.manifest.prompt_id.endswith(".system")
        assert subgraph.manifest.prompt_version == "v1"
        assert subgraph.prompt
        assert subgraph.graph.get_graph().nodes


def test_subgraph_injects_agent_identity_into_gateway() -> None:
    gateway = FakeToolGateway()
    research = build_research(gateway)
    result = asyncio.run(
        research.invoke_tool(
            ToolRequest("quantitative.get_prediction", {"security_id": "US:NASDAQ:NVDA"})
        )
    )
    assert result.payload["tool_id"] == "quantitative.get_prediction"
