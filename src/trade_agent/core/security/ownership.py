"""默认 owner 资源访问策略。"""

from .contracts import AccessPolicy, UserContext


class OwnerAccessPolicy(AccessPolicy):
    """只允许认证主体访问自己拥有的资源。

    该默认实现不包含管理员角色或租户例外。需要扩展授权时，应在组合根注入另一个
    ``AccessPolicy`` 实现，而不是在领域代码中加入角色名称判断。
    """

    def authorize(self, actor: UserContext, resource_owner_id: str) -> None:
        """校验 actor 与资源 owner 完全一致。"""

        if actor.user_id != resource_owner_id:
            raise PermissionError("资源不存在或当前用户无权访问")


__all__ = ["OwnerAccessPolicy"]
