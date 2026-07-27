"""Tool 调用中的受信身份绑定辅助函数。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from trade_agent.core.llm.contracts import JsonValue

from .contracts import ToolErrorCode, ToolExecutionError, ToolManifest, ToolRequest

OWNER_ID_FIELD = "owner_id"
ACTOR_OWNER_ID_FIELD = "actor_owner_id"
ACTOR_ID_FIELD = "actor_id"
TRUSTED_IDENTITY_FIELDS = frozenset({OWNER_ID_FIELD, ACTOR_OWNER_ID_FIELD, ACTOR_ID_FIELD})


def identity_fields_for_manifest(manifest: ToolManifest) -> frozenset[str]:
    """返回 manifest 显式声明的身份字段。

    这里只识别当前仓库约定的受信字段名，避免把普通业务字符串字段误当成身份。
    """

    properties = manifest.input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return frozenset()
    return frozenset(field for field in TRUSTED_IDENTITY_FIELDS if field in properties)


def bind_trusted_identity(
    request: ToolRequest,
    *,
    identity_fields: Iterable[str] = (),
    require_context_for_owner_scope: bool = False,
) -> ToolRequest:
    """把受信上下文中的主体身份绑定到工具参数。

    Args:
        request: 原始工具请求。
        identity_fields: 调用方已知的身份字段集合，通常来自 manifest schema。
        require_context_for_owner_scope: 若工具涉及 ``owner_id`` 或 ``actor_owner_id``，
            是否强制要求存在受信上下文。

    Returns:
        一个参数中已注入受信身份的新 ``ToolRequest``；若无需处理则返回原对象。

    Raises:
        ToolExecutionError: 当显式传入的身份与受信上下文不一致，或 owner 作用域工具缺少
            受信上下文时抛出。
    """

    fields = set(identity_fields)
    fields.update(field for field in TRUSTED_IDENTITY_FIELDS if field in request.arguments)
    if not fields:
        return request

    owner_scoped = OWNER_ID_FIELD in fields or ACTOR_OWNER_ID_FIELD in fields
    if request.context is None:
        if require_context_for_owner_scope and owner_scoped:
            raise ToolExecutionError(
                ToolErrorCode.FORBIDDEN,
                "owner 作用域工具必须提供受信执行上下文",
            )
        return request

    trusted_values = {
        OWNER_ID_FIELD: request.context.principal.owner_id,
        ACTOR_OWNER_ID_FIELD: request.context.principal.owner_id,
        ACTOR_ID_FIELD: request.context.principal.resolved_actor_id,
    }
    arguments: dict[str, JsonValue] = dict(request.arguments)
    for field in fields:
        trusted_value = trusted_values[field]
        supplied_value = request.arguments.get(field)
        if supplied_value is not None and supplied_value != trusted_value:
            raise ToolExecutionError(
                ToolErrorCode.FORBIDDEN,
                f"{field} 必须与受信执行主体一致",
            )
        arguments[field] = trusted_value

    return ToolRequest(
        tool_id=request.tool_id,
        arguments=arguments,
        idempotency_key=request.idempotency_key,
        agent_id=request.agent_id,
        approval_interaction_id=request.approval_interaction_id,
        context=request.context,
    )


__all__ = [
    "ACTOR_ID_FIELD",
    "ACTOR_OWNER_ID_FIELD",
    "OWNER_ID_FIELD",
    "TRUSTED_IDENTITY_FIELDS",
    "bind_trusted_identity",
    "identity_fields_for_manifest",
]
