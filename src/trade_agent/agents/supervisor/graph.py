"""Supervisor 的最小可审计 LangGraph 骨架。

这个图目前刻意保持“瘦”：它只做输入清洗、意图确认、路由和策略门禁，不直接在图里
拼装业务事实或调用外部 provider。职责按以下三层分离：

1. Graph 只负责决定“下一步谁来处理”；
2. ToolGateway 负责决定“是否允许做这件事”；
3. capability/application 负责决定“业务上怎么做才对”。
"""

from collections.abc import Callable, Hashable

from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from trade_agent.core.runtime import (
    DEFAULT_CLARIFICATION_AGENT_ID,
    AgentManifest,
    AgentRouteRegistry,
    AgentState,
    ErrorSummary,
    Intent,
    IntentSchema,
    normalize_route_intent,
    validate_checkpoint_state,
)

type RouteRegistrations = AgentRouteRegistry | tuple[AgentManifest, ...]

_CORE_NODE_IDS = frozenset(
    {
        "ingest",
        "classify",
        "resolve_context",
        "route",
        DEFAULT_CLARIFICATION_AGENT_ID,
        "policy_gate",
        "execute_command",
        "render",
    }
)


def ingest(state: AgentState) -> AgentState:
    """校验运行所需最小字段，并清洗用户消息。"""

    validate_checkpoint_state(state)
    required = ("user_id", "thread_id", "run_id", "message")
    missing = [field for field in required if not state.get(field)]
    if missing:
        return {
            "error_summary": ErrorSummary("invalid_ingest", f"缺少运行字段: {', '.join(missing)}")
        }
    return {"message": state["message"].strip()}


def classify(state: AgentState) -> AgentState:
    """把输入状态收敛为一个稳定 IntentSchema。

    第一版大多由上游显式写入 ``intent``；缺失时安全回退到 clarification，
    避免让运行时在没有把握的情况下假设用户想执行某个业务动作。
    """

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
    """把结构化意图映射为具体 Agent 节点名。"""

    return {"selected_agent_id": normalize_route_intent(state.get("intent"))}


def _make_route_selector(registry: AgentRouteRegistry) -> Callable[[AgentState], str]:
    """返回一个只允许跳转到已注册业务节点的条件边选择器。

    即使状态里被写入了未知字符串，也会回退到 clarification，保证图不会跳到
    未注册节点。
    """

    def select_route(state: AgentState) -> str:
        return registry.resolve(state.get("selected_agent_id"))

    return select_route


def _build_route_node(agent_id: str) -> Callable[[AgentState], AgentState]:
    """为一个业务 Agent 生成无副作用的固定路由节点。"""

    def route_to_agent(_: AgentState) -> AgentState:
        return {"selected_agent_id": agent_id}

    return route_to_agent


def clarification(_: AgentState) -> AgentState:
    return {"selected_agent_id": DEFAULT_CLARIFICATION_AGENT_ID}


def policy_gate(state: AgentState) -> AgentState:
    """在执行 Tool 前做最后一次总策略判定。"""

    if "error_summary" in state:
        return {"policy_decision": "denied"}
    if state.get("selected_agent_id") == DEFAULT_CLARIFICATION_AGENT_ID:
        return {"policy_decision": "clarification_required"}
    return {"policy_decision": "allowed"}


def select_policy_path(state: AgentState) -> str:
    """把策略判定转换为“执行”或“只渲染”的分支。"""

    return "execute_command" if state.get("policy_decision") == "allowed" else "render"


def execute_command(_: AgentState) -> AgentState:
    """命令细节通过注入的 ToolGateway 执行; checkpoint 只收结果引用。"""
    return {}


def render(state: AgentState) -> AgentState:
    """最终渲染节点。

    当前骨架阶段只验证 checkpoint 状态是否自洽；真正的 Card 由具体 Workflow 的
    presenter 生成，并通过 ``ConversationRuntime`` 持久化和发布。
    """

    validate_checkpoint_state(state)
    return {}


def _add_node(
    builder: StateGraph[AgentState, None, AgentState, AgentState],
    name: str,
    node: Callable[[AgentState], AgentState],
) -> None:
    # RunnableLambda 保留动态注册能力，同时为 LangGraph 暴露明确的输入输出类型。
    builder.add_node(name, RunnableLambda[AgentState, AgentState](node))


def _resolve_route_registry(routes: RouteRegistrations | None) -> AgentRouteRegistry:
    """把调用方传入的注册配置统一收敛为 ``AgentRouteRegistry``。"""

    if routes is None:
        from . import ROUTE_REGISTRY

        return ROUTE_REGISTRY
    if isinstance(routes, AgentRouteRegistry):
        return routes
    return AgentRouteRegistry.from_manifests(routes)


def _validate_route_node_ids(registry: AgentRouteRegistry) -> None:
    """拒绝与 Supervisor 核心节点同名的业务 Agent ID。"""

    conflicts = sorted(_CORE_NODE_IDS & set(registry.route_ids))
    if conflicts:
        joined = ", ".join(conflicts)
        raise ValueError(f"业务 Agent ID 与 Supervisor 核心节点冲突: {joined}")


def build_supervisor_graph(
    routes: RouteRegistrations | None = None,
) -> CompiledStateGraph[AgentState, None, AgentState, AgentState]:
    """按固定顺序构造 Supervisor 图。

    阅读顺序可以理解为：

    ``ingest -> classify -> resolve_context -> route -> policy_gate -> execute/render``

    这条链路只回答“该由谁处理”和“允不允许继续”，不回答具体业务规则。
    """

    registry = _resolve_route_registry(routes)
    _validate_route_node_ids(registry)

    builder: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    nodes = {
        "ingest": ingest,
        "classify": classify,
        "resolve_context": resolve_context,
        "route": route,
        "clarification": clarification,
        "policy_gate": policy_gate,
        "execute_command": execute_command,
        "render": render,
    }
    nodes.update({agent_id: _build_route_node(agent_id) for agent_id in registry.route_ids})
    for name, node in nodes.items():
        _add_node(builder, name, node)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "classify")
    builder.add_edge("classify", "resolve_context")
    builder.add_edge("resolve_context", "route")
    route_map: dict[Hashable, str] = {agent_id: agent_id for agent_id in registry.route_ids}
    route_map[DEFAULT_CLARIFICATION_AGENT_ID] = DEFAULT_CLARIFICATION_AGENT_ID
    builder.add_conditional_edges("route", _make_route_selector(registry), route_map)
    for route_node in route_map.values():
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
