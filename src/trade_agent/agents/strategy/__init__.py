"""Strategy Agent 的公开清单。

它面向“如何形成候选策略与扫描任务”，不拥有数据库访问权。
所有副作用都必须经过受控 Tool。
"""

from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

MANIFEST = AgentManifest(
    agent_id="strategy",
    description="策略草稿与量化扫描编排",
    model_route=ModelRoute("strategy_drafter"),
    # Strategy 关注策略发布与扫描，不直接操作计划审批。
    allowed_tool_ids=(
        "strategy.publish",
        "quantitative.submit_scan",
        "quantitative.get_scan_status",
        "quantitative.list_scan_results",
        "watchlist.freeze_universe",
    ),
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
)

__all__ = ["MANIFEST", "SYSTEM_PROMPT"]
