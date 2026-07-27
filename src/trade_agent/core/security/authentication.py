"""认证边界与 token verifier 抽象。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from trade_agent.core.config import AppSettings

from .contracts import UserContext


@dataclass(frozen=True, slots=True)
class AuthenticationError(Exception):
    """认证阶段向上游暴露的稳定异常。

    Attributes:
        message: 供 API 或 CLI 直接返回的错误说明。
        status_code: HTTP 语义状态码，默认 401。
    """

    message: str
    status_code: int = 401

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class VerifiedToken:
    """通过校验后的访问令牌视图。

    Attributes:
        subject: 认证主体 ID，最终映射为系统用户标识。
        roles: 从 token 提取出的角色集合。
        claims: 原始 claims 的只读快照，供审计或扩展策略使用。
    """

    subject: str
    roles: frozenset[str] = frozenset()
    claims: Mapping[str, object] = field(default_factory=dict)


class TokenVerifier(Protocol):
    """把外部 Bearer token 校验为内部可信主体。

    Contract:
        - 实现方必须 fail closed；无法验证时不得返回半可信结果。
        - 返回的 subject 必须稳定且可直接用于 owner/actor 判定。

    Implemented by:
        trade_agent.adapters.authentication.oidc.PyJwtOidcTokenVerifier
    """

    def verify(self, token: str) -> VerifiedToken:
        """校验一个原始访问令牌。

        Args:
            token: 去掉 ``Bearer `` 前缀后的原始 token 字符串。

        Returns:
            已验证且可用于后续授权的 token 视图。

        Raises:
            AuthenticationError: token 缺失、伪造、过期或不满足 issuer/audience 约束。
        """
        ...


class UserContextResolver:
    """在 development / oidc 之间统一认证解析。"""

    def __init__(
        self,
        settings: AppSettings,
        token_verifier: TokenVerifier | None,
        *,
        correlation_id_factory: Callable[[], str],
    ) -> None:
        self._authentication = settings.authentication
        self._token_verifier = token_verifier
        self._correlation_id_factory = correlation_id_factory

    def resolve(self, *, x_user_id: str | None, authorization: str | None) -> UserContext:
        if self._authentication.mode == "development":
            return self._resolve_development(x_user_id)
        return self._resolve_oidc(x_user_id, authorization)

    def _resolve_development(self, x_user_id: str | None) -> UserContext:
        user_id = x_user_id or self._authentication.development_user_id
        if not user_id:
            raise AuthenticationError("缺少认证用户")
        return UserContext(user_id, self._correlation_id_factory())

    def _resolve_oidc(self, x_user_id: str | None, authorization: str | None) -> UserContext:
        if x_user_id is not None:
            raise AuthenticationError("oidc 模式禁止使用 X-User-ID")
        token = _extract_bearer_token(authorization)
        verifier = self._token_verifier
        if verifier is None:
            raise AuthenticationError("认证服务不可用", status_code=503)
        verified = verifier.verify(token)
        return UserContext(verified.subject, self._correlation_id_factory(), verified.roles)


def _extract_bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise AuthenticationError("缺少 Authorization Bearer token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("Authorization 必须是 Bearer token")
    return token.strip()
