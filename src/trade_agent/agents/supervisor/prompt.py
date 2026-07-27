"""Supervisor 的版本化系统提示。"""

from collections.abc import Iterable

from trade_agent.core.runtime import AgentManifest

PROMPT_ID = "supervisor.system"
PROMPT_VERSION = "v1"


def build_system_prompt(manifests: Iterable[AgentManifest]) -> str:
    """根据当前注册的业务 Agent 清单生成 Supervisor 提示词。

    Args:
        manifests: 当前部署可路由到的业务 Agent manifest。

    Returns:
        只描述“如何路由”而不绑定具体能力实现的系统提示。
    """

    registered = tuple(manifests)
    if not registered:
        route_summary = "当前没有注册业务 Agent，所有不确定请求都必须进入 clarification。"
    else:
        responsibilities = "；".join(
            f"{manifest.agent_id}: {manifest.description}" for manifest in registered
        )
        route_summary = (
            "只能在以下已注册业务 Agent 中路由："
            f"{', '.join(manifest.agent_id for manifest in registered)}。"
            f"各 Agent 职责：{responsibilities}。"
        )
    return (
        "你是路由 supervisor，只负责分类、选择已注册业务 Agent，并汇总已经验证的结果。"
        f"{route_summary}"
        "不执行 tool、不创造金融事实；请求有歧义、缺少已注册目标或没有把握时进入 clarification。"
    )


SYSTEM_PROMPT = build_system_prompt(())

__all__ = ["PROMPT_ID", "PROMPT_VERSION", "SYSTEM_PROMPT", "build_system_prompt"]
