"""Enterprise-IdP claim shapes map onto the identity contract
{tenant, role, sub} via the CLAIM_*_PATH envs — AuthN stays with the inbound
JWT authorizer, and a configured-but-absent claim yields no identity value
(never a fallback to the Cognito names): resolve_role sends an unknown tenant
to the deny-all floor and an empty role in a known tenant to that tenant's
most restricted policy."""

from data_agent import governance
from data_agent.scheduling import claims


def test_cognito_names_are_the_default():
    out = claims.normalize({"custom:tenant": "exchange-a", "custom:role": "junior", "sub": "u-1"})
    assert out == {"tenant": "exchange-a", "role": "junior", "sub": "u-1"}


def test_bare_names_are_the_fallback():
    out = claims.normalize({"tenant": "ops", "role": "finops"})
    assert out["tenant"] == "ops" and out["role"] == "finops" and out["sub"] == ""


def test_nested_paths_traverse_objects_and_lists(monkeypatch):
    monkeypatch.setenv("CLAIM_TENANT_PATH", "corp.tenant")
    monkeypatch.setenv("CLAIM_ROLE_PATH", "realm_access.roles.0")
    monkeypatch.setenv("CLAIM_SUB_PATH", "oid")
    decoded = {
        "corp": {"tenant": "exchange-a"},
        "realm_access": {"roles": ["investigator", "reader"]},
        "oid": "aad-user-7",
        "custom:tenant": "WRONG",  # pinned paths must not fall back to this
    }
    out = claims.normalize(decoded)
    assert out == {"tenant": "exchange-a", "role": "investigator", "sub": "aad-user-7"}


def test_a_url_style_claim_name_is_a_literal_not_a_traversal(monkeypatch):
    monkeypatch.setenv("CLAIM_TENANT_PATH", "https://idp.example/tenant")
    out = claims.normalize({"https://idp.example/tenant": "exchange-a"})
    assert out["tenant"] == "exchange-a"


def test_a_configured_but_absent_claim_fails_closed(monkeypatch):
    monkeypatch.setenv("CLAIM_TENANT_PATH", "corp.tenant")
    monkeypatch.setenv("CLAIM_ROLE_PATH", "corp.role")
    out = claims.normalize({"custom:tenant": "ops", "custom:role": "admin"})
    # the pinned paths are authoritative — no fallback to the Cognito names,
    # so a token in the wrong shape carries no identity at all
    assert out["tenant"] == "" and out["role"] == ""
    assert governance.resolve_role(out) == governance.DENY_ALL_ROLE


def test_an_empty_role_in_a_known_tenant_gets_the_most_restricted_policy(monkeypatch):
    monkeypatch.setenv("CLAIM_ROLE_PATH", "corp.role")
    out = claims.normalize({"custom:tenant": "ops"})
    assert out["role"] == ""
    resolved = governance.resolve_role(out)  # tenant floor, never the admin role
    assert resolved.startswith("ops:") and resolved != "ops:admin"


def test_a_non_scalar_claim_value_is_no_identity(monkeypatch):
    monkeypatch.setenv("CLAIM_TENANT_PATH", "corp")
    out = claims.normalize({"corp": {"tenant": "x"}})
    assert out["tenant"] == ""


def test_both_surfaces_share_the_mapping(monkeypatch):
    import base64
    import json

    from data_agent.scheduling import mcp as mcp_mod
    from data_agent.scheduling import runtime

    monkeypatch.setenv("CLAIM_TENANT_PATH", "corp.tenant")
    monkeypatch.setenv("CLAIM_ROLE_PATH", "corp.role")

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    token = f"{part({'alg': 'RS256'})}.{part({'corp': {'tenant': 'ops', 'role': 'secops'}})}.sig"
    headers = {"Authorization": f"Bearer {token}", "authorization": f"Bearer {token}"}
    ctx = type("Ctx", (), {"request_headers": headers})()
    assert runtime.resolve_claims({}, ctx)["role"] == "secops"
    assert mcp_mod._claims_from_headers(headers)["role"] == "secops"
