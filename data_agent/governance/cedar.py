"""Persona source allowlists → AgentCore Gateway Cedar policies (the platform half).

The outbound dual allowlist has two enforcement points: the agent
refuses a source its persona does not list (`policies.source_allowed`, before
any outbound call) and the Gateway's policy engine refuses the same call at
the platform (Cedar, evaluated per tool invocation). Two enforcement points
must not mean two definitions: this module RENDERS the Gateway's Cedar
policies from the same persona `sources` fields the agent reads, so the two
halves cannot drift. A persona edit followed by `scripts/setup_gateway_policy.py`
updates both.

Facts the rendering relies on (AgentCore Policy, CUSTOM_JWT gateway):

* the principal is `AgentCore::OAuthUser`; verified JWT claims arrive as
  entity tags, read with `principal.hasTag("<claim>")` /
  `principal.getTag("<claim>")` — here the same tenant/role claims
  `scheduling/claims.py` resolves (Cognito `custom:tenant` / `custom:role` by
  default, `CLAIM_TENANT_PATH` / `CLAIM_ROLE_PATH` when another IdP is used);
* the action is `AgentCore::Action::"<target>___<tool>"`, and the resource
  must be the exact gateway ARN when the action is specific;
* an engine attached in ENFORCE mode is deny-by-default, so only `permit`
  policies are rendered: a tool no persona may call gets no policy at all.

Pure functions, no AWS calls — the script does the API work, the unit tests
pin the rendering.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MAX_NAME = 48  # AgentCore policy names: letters, digits, underscore, <= 48
NAME_PREFIX = "da_permit_"  # every policy this module owns; others are left alone


@dataclass(frozen=True)
class CedarPolicy:
    name: str
    statement: str
    description: str


def claim_names() -> tuple[str, str]:
    """The tenant/role claim names the Gateway sees as principal tags. A
    configured CLAIM_*_PATH is used verbatim when it is a literal claim name
    (Cedar tags are flat; a nested path cannot be expressed as one tag)."""
    tenant = os.environ.get("CLAIM_TENANT_PATH") or "custom:tenant"
    role = os.environ.get("CLAIM_ROLE_PATH") or "custom:role"
    return tenant, role


def allowed_targets(policy: dict, all_targets: list[str]) -> list[str]:
    """The gateway targets one persona may call — the SAME rule as
    `policies.visible_sources()`: an explicit `sources` list is exact; no field
    means every target for the unrestricted role and none for a restricted
    one; `deny_all` reaches nothing."""
    if policy.get("deny_all", False):
        return []
    sources = policy.get("sources")
    if sources is None:
        unrestricted = policy.get("allow_raw_paths", False) and not policy.get("tables")
        return sorted(all_targets) if unrestricted else []
    return sorted(t for t in all_targets if t in set(sources))


def _cedar_str(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.:@/+=-]*", value):
        # persona keys and claim names are operator configuration; anything
        # outside this set would need escaping we do not want to get wrong
        raise ValueError(f"refusing to render {value!r} into a Cedar policy")
    return f'"{value}"'


def _persona_condition(persona: str, tenant_claim: str, role_claim: str) -> str:
    tenant, _, role = persona.partition(":")
    if not tenant or not role:
        raise ValueError(f"persona key {persona!r} must be tenant:role")
    t, r = _cedar_str(tenant_claim), _cedar_str(role_claim)
    return (
        f"(principal.hasTag({t}) && principal.getTag({t}) == {_cedar_str(tenant)} && "
        f"principal.hasTag({r}) && principal.getTag({r}) == {_cedar_str(role)})"
    )


def _policy_name(action: str) -> str:
    raw = NAME_PREFIX + action.replace("___", "_").replace("-", "_")
    clean = "".join(ch for ch in raw if ch.isalnum() or ch == "_")
    if len(clean) <= MAX_NAME:
        return clean
    import hashlib

    return clean[: MAX_NAME - 7] + "_" + hashlib.sha256(clean.encode()).hexdigest()[:6]


def render(
    personas: dict[str, dict],
    tools_by_target: dict[str, list[str]],
    gateway_arn: str,
    claims: tuple[str, str] | None = None,
) -> list[CedarPolicy]:
    """One `permit` per gateway tool, naming every persona allowed to call it.

    `personas` is the governance policy table (`policies.POLICIES`);
    `tools_by_target` maps a gateway target name to its tool names. Personas
    that are not `tenant:role` keys (the built-in `deny-all`) are skipped —
    they reach nothing by construction."""
    tenant_claim, role_claim = claims or claim_names()
    targets = sorted(tools_by_target)
    callers: dict[str, list[str]] = {t: [] for t in targets}
    for persona, policy in sorted(personas.items()):
        if ":" not in persona:
            continue
        for target in allowed_targets(policy, targets):
            callers[target].append(persona)
    out: list[CedarPolicy] = []
    for target in targets:
        if not callers[target]:
            continue  # deny-by-default: no permit means the engine refuses
        when = " ||\n    ".join(
            _persona_condition(p, tenant_claim, role_claim) for p in callers[target]
        )
        for tool in sorted(tools_by_target[target]):
            action = f"{target}___{tool}"
            out.append(
                CedarPolicy(
                    name=_policy_name(action),
                    statement=(
                        "permit(\n"
                        "  principal is AgentCore::OAuthUser,\n"
                        f"  action == AgentCore::Action::{_cedar_str(action)},\n"
                        f"  resource == AgentCore::Gateway::{_cedar_str(gateway_arn)}\n"
                        ")\n"
                        f"when {{\n    {when}\n}};"
                    ),
                    description=(
                        f"{action}: callable by {', '.join(callers[target])} "
                        "(rendered from persona `sources`)"
                    ),
                )
            )
    return out
