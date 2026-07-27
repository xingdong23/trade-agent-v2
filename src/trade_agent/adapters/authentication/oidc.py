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
    """

    issuer: str
    jwks_uri: str
    signing_algorithms: tuple[str, ...]


class PyJwtOidcTokenVerifier(TokenVerifier):
    """生产 OIDC verifier, 默认 fail closed。"""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        discovery_timeout_seconds: float = 5.0,
        jwks_timeout_seconds: float = 5.0,
    ) -> None:
        metadata = _load_provider_metadata(issuer, timeout_seconds=discovery_timeout_seconds)
        self._issuer = metadata.issuer
        self._audience = audience
        self._signing_algorithms = metadata.signing_algorithms
        self._jwks_client = PyJWKClient(
            metadata.jwks_uri,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=300,
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
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise AuthenticationError("OIDC token 校验失败") from exc
        return VerifiedToken(
            subject=_required_string_claim(claims, "sub"),
            roles=_extract_roles(claims),
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
    if not isinstance(value, list):
        raise AuthenticationError("OIDC discovery 缺少签名算法列表", status_code=503)
    algorithms = tuple(item for item in value if isinstance(item, str) and item)
    if not algorithms:
        raise AuthenticationError("OIDC discovery 缺少签名算法列表", status_code=503)
    return algorithms


def _extract_roles(claims: Mapping[str, object]) -> frozenset[str]:
    raw_roles = claims.get("roles")
    if isinstance(raw_roles, list):
        return frozenset(item for item in raw_roles if isinstance(item, str) and item)
    raw_scope = claims.get("scope")
    if isinstance(raw_scope, str):
        return frozenset(item for item in raw_scope.split(" ") if item)
    return frozenset()


def _required_string_claim(claims: Mapping[str, object], field: str) -> str:
    value = claims.get(field)
    if isinstance(value, str) and value:
        return value
    raise AuthenticationError(f"OIDC token 缺少 {field} claim")


def _required_string_value(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if isinstance(value, str) and value:
        return value
    raise AuthenticationError(f"OIDC discovery 缺少 {field}", status_code=503)


def _normalize_issuer(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    return parsed.netloc.lower(), parsed.path.rstrip("/")
