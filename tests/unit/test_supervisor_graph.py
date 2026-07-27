"""Supervisor 显式 node、条件 edge 与状态路由测试。"""

from trade_agent.agents.supervisor.graph import build_supervisor_graph
from trade_agent.agents.supervisor.prompt import build_system_prompt
from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest, AgentRouteRegistry, AgentState, Intent


def _state(intent: Intent) -> AgentState:
    return {
        "user_id": "owner-a",
        "thread_id": "thread-1",
        "run_id": "run-1",
        "message": "分析 NVDA",
        "intent": intent,
    }


def test_graph_exposes_all_explicit_nodes_and_conditional_edges() -> None:
    graph = build_supervisor_graph()
    nodes = set(graph.get_graph().nodes)
    assert {
        "ingest",
        "classify",
        "resolve_context",
        "route",
        "research",
        "strategy",
        "planning",
        "clarification",
        "policy_gate",
        "execute_command",
        "render",
    } <= nodes
    conditional_sources = {
        edge.source for edge in graph.get_graph().edges if edge.conditional is True
    }
    assert {"route", "policy_gate"} <= conditional_sources


def test_graph_routes_all_supported_intents_and_safe_clarification() -> None:
    graph = build_supervisor_graph()
    for intent in Intent:
        result = graph.invoke(_state(intent))
        assert result["selected_agent_id"] == intent.value
        expected = "clarification_required" if intent is Intent.CLARIFICATION else "allowed"
        assert result["policy_decision"] == expected

    unclassified = _state(Intent.RESEARCH)
    del unclassified["intent"]
    result = graph.invoke(unclassified)
    assert result["selected_agent_id"] == "clarification"


def test_graph_builds_custom_route_nodes_from_registered_manifests() -> None:
    watchlist = AgentManifest(
        agent_id="watchlist",
        description="观察列表维护与筛选",
        model_route=ModelRoute("watchlist_curator"),
        allowed_tool_ids=("watchlist.append_symbol",),
        prompt_id="watchlist.system",
        prompt_version="v1",
    )
    graph = build_supervisor_graph(AgentRouteRegistry.from_manifests((watchlist,)))

    nodes = set(graph.get_graph().nodes)
    assert "watchlist" in nodes
    assert "research" not in nodes
    result = graph.invoke(
        {
            "user_id": "owner-a",
            "thread_id": "thread-1",
            "run_id": "run-1",
            "message": "把 NVDA 加入观察列表",
            "intent": "watchlist",
        }
    )
    assert result["selected_agent_id"] == "watchlist"
    assert result["policy_decision"] == "allowed"


def test_supervisor_prompt_describes_registered_routes_instead_of_fixed_agent_names() -> None:
    watchlist = AgentManifest(
        agent_id="watchlist",
        description="观察列表维护与筛选",
        model_route=ModelRoute("watchlist_curator"),
        allowed_tool_ids=("watchlist.append_symbol",),
        prompt_id="watchlist.system",
        prompt_version="v1",
    )

    prompt = build_system_prompt((watchlist,))

    assert "watchlist" in prompt
    assert "观察列表维护与筛选" in prompt
    assert "Research、Strategy 或 Planning" not in prompt
