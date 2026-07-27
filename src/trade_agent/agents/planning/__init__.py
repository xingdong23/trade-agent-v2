"""Planning Agent 的公开清单。

这里不放业务实现，只声明 Planning Agent 是谁、允许调用哪些 Tool、
应该走哪条模型路由。课堂上可把它看成“Agent 身份证”。
"""

from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

# MANIFEST 是 Supervisor 与 ToolPolicy 读取的稳定配置，而不是运行时临时对象。
MANIFEST = AgentManifest(
    agent_id="planning",
    description="交易计划、提醒与复盘草稿",
    model_route=ModelRoute("planning_drafter"),
    # Planning 只能访问计划与提醒相关 Tool，不能直接研究行情或执行量化扫描。
    allowed_tool_ids=(
        "planning.create_plan_draft",
        "planning.transition_plan",
        "planning.record_review",
        "reminder.create",
        "reminder.set_status",
        "reminder.get",
    ),
    prompt_id=PROMPT_ID,
    prompt_version=PROMPT_VERSION,
)

__all__ = ["MANIFEST", "SYSTEM_PROMPT"]
