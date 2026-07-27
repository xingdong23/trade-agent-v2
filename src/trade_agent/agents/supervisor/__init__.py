"""Supervisor Agent 的公开清单与业务 Agent 列表。

Supervisor 自己通常不执行业务 Tool，它的主要职责是：

1. 识别当前请求应该交给哪个业务 Agent；
2. 保证只有白名单中的 Agent 会被路由到；
3. 为容器装配和测试提供统一的业务 Agent 集合。
"""

from trade_agent.agents.planning import MANIFEST as PLANNING_AGENT
from trade_agent.agents.research import MANIFEST as RESEARCH_AGENT
from trade_agent.agents.strategy import MANIFEST as STRATEGY_AGENT
from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest, AgentRouteRegistry

from .prompt import PROMPT_ID, PROMPT_VERSION, build_system_prompt

MANIFEST = AgentManifest(
    agent_id="supervisor",
    description="意图分类、业务 Agent 路由与结果汇总",
    model_route=ModelRoute("intent_classifier"),
    # Supervisor 只做路由，不直接持有 Tool 调用权限。
    allowed_tool_ids=(),
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
)

# 这是当前系统中真正承接业务的 Agent 白名单。
BUSINESS_AGENTS = (RESEARCH_AGENT, STRATEGY_AGENT, PLANNING_AGENT)
ROUTE_REGISTRY = AgentRouteRegistry.from_manifests(BUSINESS_AGENTS)
SYSTEM_PROMPT = build_system_prompt(BUSINESS_AGENTS)

__all__ = ["BUSINESS_AGENTS", "MANIFEST", "ROUTE_REGISTRY", "SYSTEM_PROMPT"]
