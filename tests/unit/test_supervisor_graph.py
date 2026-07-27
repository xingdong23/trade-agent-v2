"""Supervisor 显式 node、条件 edge 与状态路由测试。"""

from trade_agent.agents.supervisor.graph import build_supervisor_graph
from trade_agent.core.runtime import AgentState, Intent


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
