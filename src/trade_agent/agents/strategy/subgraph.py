"""Strategy Agent 的有边界 LangGraph subgraph。"""

from langgraph.graph import END, START, StateGraph

from trade_agent.core.runtime import AgentState, AgentSubgraph
from trade_agent.core.tools import ToolGateway

from .prompt import SYSTEM_PROMPT


def _prepare(state: AgentState) -> AgentState:
    return {**state, "selected_agent_id": "strategy"}


def build_subgraph(gateway: ToolGateway) -> AgentSubgraph:
    from . import MANIFEST

    builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    builder.add_node("prepare_strategy", _prepare)
    builder.add_edge(START, "prepare_strategy")
    builder.add_edge("prepare_strategy", END)
    return AgentSubgraph(MANIFEST, SYSTEM_PROMPT, builder.compile(), gateway)


__all__ = ["build_subgraph"]
