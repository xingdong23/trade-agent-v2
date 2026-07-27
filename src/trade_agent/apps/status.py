"""骨架进程的统一状态输出。

它主要用于演示或 smoke check：快速说明当前脚手架有哪些图节点、
注册了哪些业务 Agent，而不是执行业务流程。
"""

from dataclasses import dataclass

from .container import build_scaffold_container


@dataclass(frozen=True, slots=True)
class ScaffoldStatus:
    """用于 CLI/测试输出的轻量状态快照。

    Attributes:
        process: 生成状态的进程名称。
        graph_nodes: 顶层 graph 当前包含的节点。
        business_agents: 当前注册的业务 Agent ID。
        external_calls_enabled: 是否允许访问真实外部服务。
    """

    process: str
    graph_nodes: tuple[str, ...]
    business_agents: tuple[str, ...]
    external_calls_enabled: bool = False


def scaffold_status(process: str) -> ScaffoldStatus:
    """构造一个不访问外部服务的脚手架状态视图。"""

    container = build_scaffold_container()
    return ScaffoldStatus(
        process=process,
        graph_nodes=tuple(container.graph.nodes),
        business_agents=tuple(agent.agent_id for agent in container.agents),
    )
