"""基于 OIDC discovery + JWKS 的 Bearer token 校验。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientError

from trade_agent.core.security import TokenVerifier
from trade_agent.core.security.authentication import AuthenticationError, VerifiedToken


@dataclass(frozen=True, slots=True)
class OidcProviderMetadata:
    """OIDC discovery 文档中本系统关心的最小字段集合。

    Attributes:
        issuer: provider 宣告的标准 issuer。
        jwks_uri: 用于下载签名公钥集合的端点。
        signing_algorithms: provider 允许的 token 签名算法列表。

    Invariants:
        - issuer 与 jwks_uri 必须来自同一份已验证的 discovery 文档。
        - signing_algorithms 只包含 provider 显式声明的可用签名算法。
    """

    issuer: str
    jwks_uri: str
    signing_algorithms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OidcRoleClaim:
    """描述一个角色 claim 的嵌套路径和字符串分隔规则。

    Attributes:
        path: 支持点号嵌套的 claim 路径。
        separator: 当 claim 为字符串时用于切分多个角色的分隔符；为 ``None`` 时只接受数组值。

    Invariants:
        - path 不能为空白字符串。
    """

    path: str
    separator: str | None = None

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise ValueError("OIDC role claim path 不能为空")


@dataclass(frozen=True, slots=True)
class OidcClaimMapping:
    """把 provider claims 映射为系统内部 subject 与角色集合。

    Attributes:
        subject_claim: 支持点号嵌套的 subject claim 路径。
        role_claims: 可合并多个数组或分隔字符串 claim 的提取规则。

    Invariants:
        - subject_claim 不能为空白字符串。
        - role_claims 的顺序决定角色合并时的读取顺序，但不会影响最终去重结果。
    """

    subject_claim: str
    role_claims: tuple[OidcRoleClaim, ...]

    def __post_init__(self) -> None:
        if not self.subject_claim.strip():
            raise ValueError("OIDC subject claim path 不能为空")

    def subject(self, claims: Mapping[str, object]) -> str:
        """提取非空 subject，缺失或类型错误时 fail closed。"""

        value = _claim_value(claims, self.subject_claim)
        if isinstance(value, str) and value:
            return value
        raise AuthenticationError(f"OIDC token 缺少 {self.subject_claim} claim")

    def roles(self, claims: Mapping[str, object]) -> frozenset[str]:
        """合并配置声明的角色数组与 scope 字符串。"""

        roles: set[str] = set()
        for spec in self.role_claims:
            value = _claim_value(claims, spec.path)
            if isinstance(value, list):
                roles.update(item for item in value if isinstance(item, str) and item)
            elif isinstance(value, str) and spec.separator is not None:
                roles.update(item for item in value.split(spec.separator) if item)
        return frozenset(roles)


class PyJwtOidcTokenVerifier(TokenVerifier):
    """生产 OIDC verifier, 默认 fail closed。"""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        discovery_timeout_seconds: float,
        jwks_timeout_seconds: float,
        jwks_cache_ttl_seconds: int,
        claim_mapping: OidcClaimMapping,
        required_claims: tuple[str, ...],
        signing_algorithms: tuple[str, ...],
    ) -> None:
        metadata = _load_provider_metadata(issuer, timeout_seconds=discovery_timeout_seconds)
        self._issuer = metadata.issuer
        self._audience = audience
        self._signing_algorithms = signing_algorithms or metadata.signing_algorithms
        if not self._signing_algorithms:
            raise AuthenticationError("OIDC 未配置可用签名算法", status_code=503)
        if not required_claims:
            raise ValueError("OIDC required_claims 不能为空")
        self._required_claims = required_claims
        self._claim_mapping = claim_mapping
        self._jwks_client = PyJWKClient(
            metadata.jwks_uri,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=jwks_cache_ttl_seconds,
            timeout=jwks_timeout_seconds,
        )

    def verify(self, token: str) -> VerifiedToken:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=self._signing_algorithms,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": list(self._required_claims)},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise AuthenticationError("OIDC token 校验失败") from exc
        return VerifiedToken(
            subject=self._claim_mapping.subject(claims),
            roles=self._claim_mapping.roles(claims),
            claims=dict(claims),
        )


def _load_provider_metadata(issuer: str, *, timeout_seconds: float) -> OidcProviderMetadata:
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urlopen(discovery_url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise AuthenticationError("OIDC discovery 加载失败", status_code=503) from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("OIDC discovery 响应非法", status_code=503)
    metadata_issuer = _required_string_value(payload, "issuer")
    if _normalize_issuer(metadata_issuer) != _normalize_issuer(issuer):
        raise AuthenticationError("OIDC issuer 与 discovery 不匹配", status_code=503)
    jwks_uri = _required_string_value(payload, "jwks_uri")
    signing_algorithms = _parse_signing_algorithms(
        payload.get("id_token_signing_alg_values_supported")
    )
    return OidcProviderMetadata(metadata_issuer, jwks_uri, signing_algorithms)


def _parse_signing_algorithms(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise AuthenticationError("OIDC discovery 签名算法列表非法", status_code=503)
    algorithms = tuple(item for item in value if isinstance(item, str) and item)
    if not algorithms:
        raise AuthenticationError("OIDC discovery 缺少签名算法列表", status_code=503)
    return algorithms


def _claim_value(claims: Mapping[str, object], path: str) -> object:
    value: object = claims
    for segment in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(segment)
    return value


def _required_string_value(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if isinstance(value, str) and value:
        return value
    raise AuthenticationError(f"OIDC discovery 缺少 {field}", status_code=503)


def _normalize_issuer(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    return parsed.netloc.lower(), parsed.path.rstrip("/")
