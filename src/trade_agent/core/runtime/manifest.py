"""Agent declarations shared without importing capability implementations."""

from dataclasses import dataclass

from trade_agent.core.llm import ModelRoute


@dataclass(frozen=True, slots=True)
class AgentManifest:
    """声明一个业务子智能体的稳定运行合同。

    Attributes:
        agent_id: 子智能体的稳定协议 ID。
        description: 供编排层和前端说明用途的简述。
        model_route: 该子智能体默认使用的逻辑模型路由。
        allowed_tool_ids: 允许调用的工具协议 ID 列表。
        prompt_id: prompt 模板标识，用于审计与版本管理。
        prompt_version: prompt 内容版本。
    """

    agent_id: str
    description: str
    model_route: ModelRoute
    allowed_tool_ids: tuple[str, ...]
    prompt_id: str = "unversioned"
    prompt_version: str = "v1"
