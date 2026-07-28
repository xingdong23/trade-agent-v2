"""基于 Agent manifest 与 tool metadata 的服务端调用 policy。"""

from typing import Protocol

from .contracts import ToolError, ToolErrorCode, ToolManifest, ToolRequest


class ToolPolicy(Protocol):
    """工具调用前的授权与约束评估协议。

    Contract:
        - 评估必须是纯函数式决策，不直接执行工具副作用。
        - 返回 ``None`` 表示允许调用；返回 ``ToolError`` 表示拒绝原因。

    Implemented by:
        trade_agent.core.tools.policy.ManifestToolPolicy
    """

    def evaluate(self, request: ToolRequest, manifest: ToolManifest) -> ToolError | None:
        """根据请求与工具声明决定是否允许执行。

        Args:
            request: 当前工具调用请求。
            manifest: 被调用工具的注册声明。

        Returns:
            允许执行时返回 ``None``，否则返回稳定错误对象。
        """
        ...


class ManifestToolPolicy(ToolPolicy):
    def __init__(self, allowlists: dict[str, frozenset[str]]) -> None:
        self._allowlists = dict(allowlists)

    def evaluate(self, request: ToolRequest, manifest: ToolManifest) -> ToolError | None:
        if request.agent_id is None or request.tool_id not in self._allowlists.get(
            request.agent_id, frozenset()
        ):
            return ToolError(ToolErrorCode.FORBIDDEN, "Agent manifest 未授权该 tool")
        if manifest.requires_idempotency_key and not request.idempotency_key:
            return ToolError(
                ToolErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                "受控写操作缺少 idempotency key",
            )
        if manifest.requires_hitl and not request.approval_interaction_id:
            return ToolError(ToolErrorCode.HITL_REQUIRED, "该 tool 必须引用已解决的 HITL 交互")
        return None
