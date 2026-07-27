from trade_agent.core.llm import ModelRoute
from trade_agent.core.runtime import AgentManifest

from .prompt import PROMPT_ID, PROMPT_VERSION, SYSTEM_PROMPT

MANIFEST = AgentManifest(
    agent_id="planning",
    description="交易计划、提醒与复盘草稿",
    model_route=ModelRoute("planning_drafter"),
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
