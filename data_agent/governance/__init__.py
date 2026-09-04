"""Governance layer — authorization only: who may see what.

Authentication is AgentCore's job (the inbound JWT authorizer validates
tokens before agent code runs). This package is the authorization half: it
maps a verified principal to a role policy and rewrites every statement so
the engine only ever sees permitted rows and columns — the query-rewrite
pattern production BI engines use to inject row-level security ahead of an
embedded execution engine. Anything about what the model sees or what the
engine is asked belongs to the context layer, not here.

Two-stage mapping keeps identity churn out of policy management:
  principal (IdP-verified claims)  →  role     (tenant:role key)
  role                             →  policy   (row filter + column denylist)

    policies   the policy table, validation, role resolution, active principal
    hardening  rules for EVERY principal (DML behind a CTE, host-reaching
               functions, non-s3:// paths) — each closed a verified bypass
    rewrite    RLS/CLS rewrite for restricted principals, fail-closed

Fail-closed: for restricted principals, SQL that cannot be parsed is
rejected, statement shapes sqlglot cannot model are rejected, raw file access
is rejected, no local name may reuse a governed table's name, and every
reference to a governed logical table — through ANY catalog access method —
gets the policy applied. A principal without a policy is the deny-all role.

Tests that monkeypatch the policy table patch `governance.policies`, the
module the functions read from; the names below are re-exports.
"""

from .hardening import RAW_PATH_SCHEMES
from .policies import (
    DEFAULT_ROLE,
    DENY_ALL_ROLE,
    POLICIES,
    active_policy,
    active_role,
    is_denied_all,
    resolve_role,
    set_active_role,
)
from .rewrite import apply_governance, describe_applied, set_schema_prober

__all__ = [
    "DEFAULT_ROLE",
    "DENY_ALL_ROLE",
    "POLICIES",
    "RAW_PATH_SCHEMES",
    "active_policy",
    "active_role",
    "apply_governance",
    "describe_applied",
    "is_denied_all",
    "resolve_role",
    "set_active_role",
    "set_schema_prober",
]
