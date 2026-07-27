"""Runtime evidence for the phase-one skeleton."""

import asyncio

import pytest

from trade_agent.adapters.llm.litellm import LiteLLMClientScaffold
from trade_agent.agents.supervisor import BUSINESS_AGENTS
from trade_agent.apps.container import build_scaffold_container
from trade_agent.apps.status import scaffold_status
from trade_agent.core.llm import LLMMessage, LLMRequest, ModelRoute
from trade_agent.core.runtime import Intent


def test_only_three_business_agents_are_registered() -> None:
    assert tuple(agent.agent_id for agent in BUSINESS_AGENTS) == (
        "research",
        "strategy",
        "planning",
    )


def test_minimal_graph_compiles_and_routes_without_external_calls() -> None:
    container = build_scaffold_container()
    result = container.graph.invoke(
        {
            "user_id": "architecture-reviewer",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "message": "分析 NVDA",
            "intent": Intent.RESEARCH,
        }
    )
    assert result["intent"] == Intent.RESEARCH


def test_all_processes_share_the_scaffold_composition_root() -> None:
    for process in ("api", "cli", "worker"):
        status = scaffold_status(process)
        assert status.business_agents == ("research", "strategy", "planning")
        assert status.external_calls_enabled is False
        assert {"ingest", "classify", "policy_gate", "execute_command", "render"} <= set(
            status.graph_nodes
        )


def test_litellm_scaffold_requires_configured_route_before_sdk_call() -> None:
    client = LiteLLMClientScaffold({})
    with pytest.raises(ValueError, match="未配置逻辑模型路由"):
        asyncio.run(
            client.complete(
                LLMRequest(
                    route=ModelRoute("research_summarizer"),
                    messages=(LLMMessage("user", "研究 NVDA"),),
                    prompt_version="research.v1",
                )
            )
        )
