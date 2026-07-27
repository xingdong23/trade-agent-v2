"""Agent declarations shared without importing capability implementations."""

from dataclasses import dataclass

from trade_agent.core.llm import ModelRoute


@dataclass(frozen=True, slots=True)
class AgentManifest:
    agent_id: str
    description: str
    model_route: ModelRoute
    allowed_tool_ids: tuple[str, ...]
    prompt_id: str = "unversioned"
    prompt_version: str = "v1"
