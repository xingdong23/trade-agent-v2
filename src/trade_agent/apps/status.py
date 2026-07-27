"""Shared status output for skeleton process entry points."""

from dataclasses import dataclass

from .container import build_scaffold_container


@dataclass(frozen=True, slots=True)
class ScaffoldStatus:
    process: str
    graph_nodes: tuple[str, ...]
    business_agents: tuple[str, ...]
    external_calls_enabled: bool = False


def scaffold_status(process: str) -> ScaffoldStatus:
    container = build_scaffold_container()
    return ScaffoldStatus(
        process=process,
        graph_nodes=tuple(container.graph.nodes),
        business_agents=tuple(agent.agent_id for agent in container.agents),
    )
