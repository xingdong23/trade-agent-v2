"""Agent declarations shared without importing capability implementations."""

from collections.abc import Iterable
from dataclasses import dataclass

from trade_agent.core.llm import ModelRoute

from .contracts import DEFAULT_CLARIFICATION_AGENT_ID, RouteIntent, normalize_route_intent


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
    prompt_id: str
    prompt_version: str

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.agent_id,
                self.description,
                self.model_route.name,
                self.prompt_id,
                self.prompt_version,
            )
        ):
            raise ValueError("Agent manifest 标识、描述、模型路由与 prompt lineage 不能为空")


@dataclass(frozen=True, slots=True)
class AgentRouteRegistry:
    """保存 Supervisor 当前允许路由到的业务 Agent 集合。

    设计意图:
        - 业务 Agent 的可路由目标来自 ``AgentManifest`` 注册，而不是写死在图代码里。
        - ``clarification`` 始终保留为框架级安全回退，不允许业务 Agent 抢占该 ID。
        - Registry 只负责校验与查找，不关心图实现、提示词拼装或 Tool 细节。
    """

    manifests: tuple[AgentManifest, ...]
    clarification_agent_id: str = DEFAULT_CLARIFICATION_AGENT_ID

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for manifest in self.manifests:
            agent_id = manifest.agent_id.strip()
            if not agent_id:
                raise ValueError("agent_id 不能为空")
            if agent_id == self.clarification_agent_id:
                raise ValueError("clarification 是保留路由, 不能作为业务 Agent ID")
            if agent_id in seen:
                raise ValueError(f"Agent 路由重复注册: {agent_id}")
            seen.add(agent_id)

    @classmethod
    def from_manifests(
        cls,
        manifests: Iterable[AgentManifest],
        *,
        clarification_agent_id: str = DEFAULT_CLARIFICATION_AGENT_ID,
    ) -> "AgentRouteRegistry":
        """从一组业务 Agent manifest 构造稳定路由注册表。"""

        return cls(tuple(manifests), clarification_agent_id)

    @property
    def route_ids(self) -> tuple[str, ...]:
        """返回当前部署注册的业务 Agent 路由 ID。"""

        return tuple(manifest.agent_id for manifest in self.manifests)

    def contains(self, agent_id: str) -> bool:
        """判断某个路由 ID 是否已在当前部署注册。"""

        return agent_id in self.route_ids

    def resolve(self, intent: RouteIntent | None) -> str:
        """把任意输入意图映射为可执行或安全降级的目标节点。"""

        candidate = normalize_route_intent(intent)
        if self.contains(candidate):
            return candidate
        return self.clarification_agent_id
