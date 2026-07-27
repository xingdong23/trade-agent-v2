from trade_agent.agents.planning import MANIFEST as PLANNING_AGENT
from trade_agent.agents.research import MANIFEST as RESEARCH_AGENT
from trade_agent.agents.strategy import MANIFEST as STRATEGY_AGENT
from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

MANIFEST = AgentManifest(
    agent_id="supervisor",
    description="意图分类、业务 Agent 路由与结果汇总",
    model_route=ModelRoute("intent_classifier"),
    allowed_tool_ids=(),
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
)

BUSINESS_AGENTS = (RESEARCH_AGENT, STRATEGY_AGENT, PLANNING_AGENT)

__all__ = ["BUSINESS_AGENTS", "MANIFEST", "SYSTEM_PROMPT"]
