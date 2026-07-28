"""Supervisor Graph 的应用层调用协议与 LangGraph 适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from langgraph.graph.state import CompiledStateGraph

from trade_agent.core.runtime import AgentState


class GraphInvoker(Protocol):
    """会话入口调用 Supervisor Graph 所需的最小协议。

    Contract:
        - 输入只能包含 ``AgentState`` 声明的小型编排值。
        - 返回值必须包含 Graph 最终确认的 ``selected_agent_id``。
        - Graph 不得把 LLM 文本当作领域事实或绕过 Workflow 注册表。

    Implemented by:
        ``SupervisorGraphInvoker`` 与测试中的显式 Graph fake。
    """

    def invoke(self, input: AgentState) -> Mapping[str, object]:
        """执行一次 Supervisor Graph 并返回最终状态。"""


@dataclass(frozen=True, slots=True)
class SupervisorGraphInvoker(GraphInvoker):
    """把 LangGraph 编译结果显式适配为会话入口协议。

    Attributes:
        graph: 已使用当前 Agent 注册表编译完成的 Supervisor Graph。

    Invariants:
        - 适配器只转发调用，不在 Graph 之后重新决定 Agent 或 Workflow。
    """

    graph: CompiledStateGraph[AgentState, None, AgentState, AgentState]

    def invoke(self, input: AgentState) -> Mapping[str, object]:
        """执行已编译 Graph，并把类型化状态暴露为只读映射。"""

        return cast(Mapping[str, object], self.graph.invoke(input))


__all__ = ["GraphInvoker", "SupervisorGraphInvoker"]
