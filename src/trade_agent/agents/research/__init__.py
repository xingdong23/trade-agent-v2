"""Research Agent 的公开清单。

Research Agent 负责“收集与解释证据”，而不是直接给出最终交易动作。
量化模型在这里表现为可调用 Tool，而不是独立 Agent。
"""

from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

MANIFEST = AgentManifest(
    agent_id="research",
    description="基于证据的美股研究与解释",
    model_route=ModelRoute("research_summarizer"),
    # 研究阶段允许解析证券、读取研究证据和量化结果，但不允许改写计划状态。
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
