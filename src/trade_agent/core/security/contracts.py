"""Security values required at application boundaries."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UserContext:
    """跨应用边界传递的调用者上下文。

    Attributes:
        user_id: 当前认证主体的稳定业务标识。
        correlation_id: 本次请求或运行链路的追踪 ID。
        roles: 认证或授权系统赋予的角色集合。
    """

    user_id: str
    correlation_id: str
    roles: frozenset[str] = frozenset()


class AccessPolicy(Protocol):
    """资源归属校验协议。

    Contract:
        - 实现方必须在拒绝访问时抛出异常，而不是返回布尔值让调用方猜测。
        - 相同输入必须产生稳定决策，不能依赖隐式全局状态。

    Implemented by:
        trade_agent.core.security.ownership.OwnerAccessPolicy
    """

    def authorize(self, actor: UserContext, resource_owner_id: str) -> None:
        """检查调用者是否可以访问某个 owner 资源。

        Args:
            actor: 当前请求的认证上下文。
            resource_owner_id: 被访问资源所属 owner。

        Raises:
            PermissionError: actor 不具备访问目标 owner 资源的权限。
        """
        ...
