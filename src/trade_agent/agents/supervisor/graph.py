"""具有显式 node 与 conditional edge 的 supervisor LangGraph。"""

from collections.abc import Callable, Hashable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from trade_agent.core.runtime import (
    AgentState,
    ErrorSummary,
    Intent,
    IntentSchema,
    validate_checkpoint_state,
)


def ingest(state: AgentState) -> AgentState:
    validate_checkpoint_state(state)
    required = ("user_id", "thread_id", "run_id", "message")
    missing = [field for field in required if not state.get(field)]
    if missing:
        return {
            "error_summary": ErrorSummary("invalid_ingest", f"缺少运行字段: {', '.join(missing)}")
        }
    return {"message": state["message"].strip()}


def classify(state: AgentState) -> AgentState:
    intent = state.get("intent")
    if intent is None:
        intent = Intent.CLARIFICATION
    confidence = 1.0 if "intent" in state else 0.0
    return {
        "intent": intent,
        "intent_result": IntentSchema(intent, confidence, "explicit_or_safe_default"),
    }


def resolve_context(state: AgentState) -> AgentState:
    """只保留 repository reference; 实际 evidence assembly 属于 capability。"""
    return {"context_references": state.get("context_references", ())}


def route(state: AgentState) -> AgentState:
    intent = state.get("intent", Intent.CLARIFICATION)
    return {"selected_agent_id": intent.value}


def select_route(state: AgentState) -> str:
    selected = state.get("selected_agent_id", Intent.CLARIFICATION.value)
    if selected not in {intent.value for intent in Intent}:
        return Intent.CLARIFICATION.value
    return selected


def research(_: AgentState) -> AgentState:
    return {"selected_agent_id": "research"}


def strategy(_: AgentState) -> AgentState:
    return {"selected_agent_id": "strategy"}


def planning(_: AgentState) -> AgentState:
    return {"selected_agent_id": "planning"}


def clarification(_: AgentState) -> AgentState:
    return {"selected_agent_id": "clarification"}


def policy_gate(state: AgentState) -> AgentState:
    if "error_summary" in state:
        return {"policy_decision": "denied"}
    if state.get("selected_agent_id") == Intent.CLARIFICATION.value:
        return {"policy_decision": "clarification_required"}
    return {"policy_decision": "allowed"}


def select_policy_path(state: AgentState) -> str:
    return "execute_command" if state.get("policy_decision") == "allowed" else "render"


def execute_command(_: AgentState) -> AgentState:
    """命令细节通过注入的 ToolGateway 执行; checkpoint 只收结果引用。"""
    return {}


def render(state: AgentState) -> AgentState:
    validate_checkpoint_state(state)
    return {}


def _add_node(
    builder: StateGraph[AgentState, None, AgentState, AgentState],
    name: str,
    node: Callable[[AgentState], AgentState],
) -> None:
    # LangGraph 1.2 的方法级 NodeInputT overload 会被 mypy 推断为 Never。
    builder.add_node(name, node)  # type: ignore[call-overload]


def build_supervisor_graph() -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    nodes = {
        "ingest": ingest,
        "classify": classify,
        "resolve_context": resolve_context,
        "route": route,
        "research": research,
        "strategy": strategy,
        "planning": planning,
        "clarification": clarification,
        "policy_gate": policy_gate,
        "execute_command": execute_command,
        "render": render,
    }
    for name, node in nodes.items():
        _add_node(builder, name, node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "classify")
    builder.add_edge("classify", "resolve_context")
    builder.add_edge("resolve_context", "route")
    routes: dict[Hashable, str] = {intent.value: intent.value for intent in Intent}
    builder.add_conditional_edges("route", select_route, routes)
    for route_node in routes.values():
        builder.add_edge(route_node, "policy_gate")
    builder.add_conditional_edges(
        "policy_gate",
        select_policy_path,
        {"execute_command": "execute_command", "render": "render"},
    )
    builder.add_edge("execute_command", "render")
    builder.add_edge("render", END)
    return builder.compile()


__all__ = [
    "build_supervisor_graph",
    "classify",
    "execute_command",
    "ingest",
    "policy_gate",
    "render",
    "resolve_context",
    "route",
]
