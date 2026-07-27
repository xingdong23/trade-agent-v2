"""业务 Agent subgraph 的供应商无关运行容器。"""

from dataclasses import dataclass

from langgraph.graph.state import CompiledStateGraph

from trade_agent.core.tools import ToolGateway, ToolRequest, ToolResult

from .contracts import AgentState
from .manifest import AgentManifest


@dataclass(frozen=True, slots=True)
class AgentSubgraph:
    """运行一个业务子智能体所需的编译后容器。

    Attributes:
        manifest: 子智能体静态声明信息。
        prompt: 注入到图中的系统提示或角色说明。
        graph: 已编译的 LangGraph 状态图。
        tool_gateway: 负责最终工具授权与执行的网关。
    """

    manifest: AgentManifest
    prompt: str
    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState]
    tool_gateway: ToolGateway

    async def invoke_tool(self, request: ToolRequest) -> ToolResult:
        """强制补充 Agent 身份, 最终授权仍由 ToolGateway 决定。"""
        scoped = ToolRequest(
            tool_id=request.tool_id,
            arguments=request.arguments,
            idempotency_key=request.idempotency_key,
            agent_id=self.manifest.agent_id,
            approval_interaction_id=request.approval_interaction_id,
            context=request.context,
        )
        return await self.tool_gateway.invoke(scoped)
