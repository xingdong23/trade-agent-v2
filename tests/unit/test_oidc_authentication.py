"""OIDC 部署参数与 claim 映射的显式配置测试。"""

from typing import Any

import pytest

from trade_agent.adapters.authentication import OidcClaimMapping, OidcRoleClaim
from trade_agent.adapters.authentication import oidc as oidc_module


def test_claim_mapping_supports_nested_subject_roles_and_custom_scope_separator() -> None:
    mapping = OidcClaimMapping(
        subject_claim="identity.subject",
        role_claims=(
            OidcRoleClaim("realm_access.roles"),
            OidcRoleClaim("permissions", separator=","),
        ),
    )
    claims: dict[str, object] = {
        "identity": {"subject": "owner-a"},
        "realm_access": {"roles": ["analyst", "reviewer"]},
        "permissions": "research,planning",
    }

    assert mapping.subject(claims) == "owner-a"
    assert mapping.roles(claims) == frozenset({"analyst", "reviewer", "research", "planning"})


def test_verifier_uses_explicit_timeouts_cache_ttl_claims_and_algorithm_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class RecordingJwksClient:
        def __init__(self, uri: str, **kwargs: object) -> None:
            captured["uri"] = uri
            captured.update(kwargs)

    monkeypatch.setattr(
        oidc_module,
        "_load_provider_metadata",
        lambda issuer, *, timeout_seconds: (
            captured.update({"issuer": issuer, "discovery_timeout_seconds": timeout_seconds})
            or oidc_module.OidcProviderMetadata(
                issuer, "https://id.example/jwks", ("discovery-alg",)
            )
        ),
    )
    monkeypatch.setattr(oidc_module, "PyJWKClient", RecordingJwksClient)

    verifier = oidc_module.PyJwtOidcTokenVerifier(
        issuer="https://id.example",
        audience="trade-agent",
        discovery_timeout_seconds=7.0,
        jwks_timeout_seconds=11.0,
        jwks_cache_ttl_seconds=900,
        claim_mapping=OidcClaimMapping("sub", (OidcRoleClaim("groups"),)),
        required_claims=("exp", "iss", "aud", "sub"),
        signing_algorithms=("explicit-alg",),
    )

    assert captured["discovery_timeout_seconds"] == 7.0
    assert captured["timeout"] == 11.0
    assert captured["lifespan"] == 900
    assert verifier._signing_algorithms == ("explicit-alg",)
