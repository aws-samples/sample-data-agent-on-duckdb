"""Claim-shape normalization shared by both inbound surfaces.

AuthN stays with the platform's inbound JWT authorizer (it verifies the
signature before agent code runs); this module only maps an already-verified
token's claims onto the identity contract {tenant, role, sub}. Cognito's
custom-attribute names are the default; another IdP's claim shape maps with
three env paths and no code edit:

    CLAIM_TENANT_PATH / CLAIM_ROLE_PATH / CLAIM_SUB_PATH

A path is tried as a LITERAL claim name first (URL-style enterprise claims
like "https://idp.example/tenant" contain dots), then as a dot-separated
traversal through nested objects and list indexes ("realm_access.roles.0").
When a path is configured it is authoritative: a token without that claim
yields an empty value and the Cognito fallbacks are NOT consulted (the
operator pinned the shape). An empty shape fails closed in resolve_role —
unknown tenant → the deny-all floor; empty role in a known tenant → that
tenant's most restricted policy.
"""

from __future__ import annotations

import os


def _lookup(decoded: dict, path: str):
    if path in decoded:  # a literal claim name, dots and slashes included
        return decoded[path]
    cur: object = decoded
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def _claim(decoded: dict, env: str, fallbacks: tuple[str, ...]) -> str:
    path = os.environ.get(env, "")
    if path:
        value = _lookup(decoded, path)
    else:
        value = next((decoded[n] for n in fallbacks if decoded.get(n)), None)
    # a claim must be a scalar; an object or list is not an identity value
    return str(value) if isinstance(value, str | int) else ""


def normalize(decoded: dict) -> dict:
    """Verified-token claims → the identity contract {tenant, role, sub}."""
    return {
        "tenant": _claim(decoded, "CLAIM_TENANT_PATH", ("custom:tenant", "tenant")),
        "role": _claim(decoded, "CLAIM_ROLE_PATH", ("custom:role", "role")),
        # `sub` is the IdP's stable per-user id — carried only to bind the
        # session to an owner (resolve_role ignores it)
        "sub": _claim(decoded, "CLAIM_SUB_PATH", ("sub",)),
    }
