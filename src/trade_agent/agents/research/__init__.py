from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

MANIFEST = AgentManifest(
    agent_id="research",
    description="基于证据的美股研究与解释",
    model_route=ModelRoute("research_summarizer"),
    allowed_tool_ids=(
        "market_research.resolve_security",
        "market_research.research_security",
        "market_research.research_theme",
        "quantitative.get_prediction",
        "quantitative.get_quantitative_snapshot",
    ),
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
)

__all__ = ["MANIFEST", "SYSTEM_PROMPT"]
