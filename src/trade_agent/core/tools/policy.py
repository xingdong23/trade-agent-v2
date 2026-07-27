"""基于 Agent manifest 与 tool metadata 的服务端调用 policy。"""

from typing import Protocol

from .contracts import ToolError, ToolErrorCode, ToolManifest, ToolRequest


class ToolPolicy(Protocol):
    def evaluate(self, request: ToolRequest, manifest: ToolManifest) -> ToolError | None: ...


class ManifestToolPolicy:
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
