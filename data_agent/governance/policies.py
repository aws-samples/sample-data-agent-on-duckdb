"""The policy table: load, validate, resolve a principal, hold the active role.

role key = "<tenant>:<role>". Loaded from GOVERNANCE_POLICIES (JSON env) if
set, so policies ship as configuration, not code; otherwise the built-in
table: the platform `ops:admin` role plus every scenario's personas
(`state.registry.default_policies()`). In a larger estate move this table to
DynamoDB — the resolve/rewrite contract stays identical.

Policy semantics per governed logical table (matched by bare table name,
catalog/schema-agnostic on purpose — the same logical table is reachable
via raw view, s3t.*, glue.* and dl.*):
  row_filter:   predicate ANDed into every access (RLS)
  deny_columns: projected away via SELECT * EXCLUDE (...) (CLS)
allow_raw_paths: False → read_parquet()/read_csv() etc. are rejected,
  otherwise path access would bypass table-level policy entirely.
deny_all: True → every statement is rejected (the floor for principals
  that carry no usable identity; see DENY_ALL_ROLE).
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading

from sqlglot import exp, parse_one

from data_agent.storage import registry

# full-access operations role: no rewrite, raw paths allowed
_BUILTIN_POLICIES = {"ops:admin": {"allow_raw_paths": True, "tables": {}}}

# The floor. Missing/blank/unknown-tenant claims land here, and so does any
# deployment that forgets GOVERNANCE_DEFAULT_ROLE. It is always present in the
# policy table (an operator-supplied table cannot remove it) and rejects every
# statement before parsing. The previous default was ops:admin — an
# unauthenticated-looking request became the unrestricted principal, which
# the Fail-Safe NFR forbids.
DENY_ALL_ROLE = "deny-all"
_DENY_ALL_POLICY = {"allow_raw_paths": False, "deny_all": True, "tables": {}}

_ROLE_KEY_RE = re.compile(r"^[^:\s]+:[^:\s]+$")  # "<tenant>:<role>", no whitespace
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # bare SQL identifier
_POLICY_KEYS = {"allow_raw_paths", "tables", "deny_all"}
_RULE_KEYS = {"row_filter", "deny_columns"}


def _validate_policies(policies: dict) -> None:
    """Fail-stop on a malformed policy table (T011/M013).

    Everything here is interpolated into SQL or compared against parsed
    identifiers, so shape errors would otherwise surface as silent
    over-exposure (a typo'd table name governs nothing) or as SQL injection
    from configuration. Rejecting at import means a bad table never serves a
    single query.
    """
    if not isinstance(policies, dict) or not policies:
        raise ValueError("governance: GOVERNANCE_POLICIES must be a non-empty JSON object")
    for key, policy in policies.items():
        where = f"policy {key!r}"
        if key != DENY_ALL_ROLE and not _ROLE_KEY_RE.match(key):
            raise ValueError(f"governance: {where}: role key must look like '<tenant>:<role>'")
        if not isinstance(policy, dict):
            raise ValueError(f"governance: {where}: must be an object")
        unknown = set(policy) - _POLICY_KEYS
        if unknown:
            raise ValueError(f"governance: {where}: unknown keys {sorted(unknown)}")
        for flag in ("allow_raw_paths", "deny_all"):
            if flag in policy and not isinstance(policy[flag], bool):
                raise ValueError(f"governance: {where}: {flag} must be a boolean")
        tables = policy.get("tables", {})
        if not isinstance(tables, dict):
            raise ValueError(f"governance: {where}: tables must be an object")
        if policy.get("allow_raw_paths") and tables:
            # a raw path read bypasses every table rule, so this combination
            # promises governance it cannot deliver
            raise ValueError(
                f"governance: {where}: allow_raw_paths=true cannot be combined with governed tables"
            )
        for tname, rule in tables.items():
            twhere = f"{where}, table {tname!r}"
            if not _IDENT_RE.match(tname):
                raise ValueError(f"governance: {twhere}: table name must be a bare identifier")
            if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
                raise ValueError(f"governance: {twhere}: rule keys must be row_filter/deny_columns")
            pred = rule.get("row_filter")
            if pred is not None:
                if not isinstance(pred, str) or not pred.strip():
                    raise ValueError(f"governance: {twhere}: row_filter must be a non-empty string")
                try:  # must be a single predicate, nothing more
                    probe = parse_one(f"SELECT * FROM t WHERE {pred}", read="duckdb")  # nosec B608 - validation-only parse, never executed
                except Exception as e:  # noqa: BLE001 — any parse failure is a bad policy
                    raise ValueError(
                        f"governance: {twhere}: row_filter does not parse ({e})"
                    ) from e
                if not isinstance(probe, exp.Select) or probe.args.get("where") is None:
                    raise ValueError(f"governance: {twhere}: row_filter must be a WHERE predicate")
            cols = rule.get("deny_columns")
            if cols is not None and (
                not isinstance(cols, list)
                or not all(isinstance(c, str) and _IDENT_RE.match(c) for c in cols)
            ):
                raise ValueError(f"governance: {twhere}: deny_columns must be bare identifiers")
    if not policies[DENY_ALL_ROLE].get("deny_all"):
        raise ValueError(f"governance: policy {DENY_ALL_ROLE!r} must keep deny_all=true")


def _default_policies() -> dict:
    return copy.deepcopy({**_BUILTIN_POLICIES, **registry.default_policies()})


def _load_policies() -> dict:
    raw = os.environ.get("GOVERNANCE_POLICIES")
    policies = json.loads(raw) if raw else _default_policies()
    if isinstance(policies, dict):
        policies.setdefault(DENY_ALL_ROLE, copy.deepcopy(_DENY_ALL_POLICY))
        # table names are matched lowercase against parsed identifiers
        for policy in policies.values():
            if isinstance(policy, dict) and isinstance(policy.get("tables"), dict):
                policy["tables"] = {k.lower(): v for k, v in policy["tables"].items()}
    _validate_policies(policies)
    return policies


POLICIES = _load_policies()
DEFAULT_ROLE = os.environ.get("GOVERNANCE_DEFAULT_ROLE", DENY_ALL_ROLE)
if DEFAULT_ROLE not in POLICIES:  # fail-stop: a typo here must not become "no policy"
    raise ValueError(f"governance: GOVERNANCE_DEFAULT_ROLE {DEFAULT_ROLE!r} is not a policy key")

# principal → active policy is per-invocation state. AgentCore serves one
# session per microVM, but a threading.local would lose the value across the
# entrypoint→tool-thread boundary (Strands runs tools on worker threads), so
# a module-level slot guarded by a lock is the correct scope here.
_active = {"role": DEFAULT_ROLE}
_active_lock = threading.Lock()


def _restrictiveness(role: str) -> tuple:
    """Sort key for "how restricted is this role" — larger is more restricted.

    Derived from the policy content itself so there is no extra metadata to
    keep in sync. deny_all outranks everything, then denying raw file access
    (a path read bypasses the table policies entirely, so it is the widest
    hole), then the number of governed tables, then row filters, then denied
    columns. Callers sort the role keys first, so ties resolve
    deterministically and the answer never depends on declaration order.
    """
    policy = POLICIES[role]
    tables = policy.get("tables", {}) or {}
    filters = sum(1 for rule in tables.values() if rule.get("row_filter"))
    denies = sum(len(rule.get("deny_columns") or []) for rule in tables.values())
    return (
        1 if policy.get("deny_all", False) else 0,
        0 if policy.get("allow_raw_paths", False) else 1,
        len(tables),
        filters,
        denies,
    )


def resolve_role(claims: dict | None) -> str:
    """Map verified identity claims to a role key (stage 1 of the mapping).

    Claims arrive from the AgentCore entrypoint: decoded JWT custom claims
    on the OAuth path, or the payload/User-Id fallback on the SigV4 path.
    Unknown tenant:role combinations fall back to the *most* restricted
    matching tenant policy if one exists, else the default role (deny-all
    unless the operator configured otherwise).
    """
    if not claims:
        return DEFAULT_ROLE
    tenant = claims.get("tenant", "")
    role = claims.get("role", "")
    key = f"{tenant}:{role}"
    if key in POLICIES:
        return key
    # Unknown role inside a known tenant → that tenant's MOST restricted policy.
    # Returning "the first key that matches the tenant" depended on dict
    # insertion order: a tenant declaring its admin policy first handed full
    # access to every unknown role. No test with one policy per tenant (the
    # built-in table) can observe that, which is why it survived.
    same_tenant = [k for k in POLICIES if k.startswith(f"{tenant}:")]
    if same_tenant:
        return max(sorted(same_tenant), key=_restrictiveness)
    return DEFAULT_ROLE


def set_active_role(role: str) -> None:
    with _active_lock:
        _active["role"] = role if role in POLICIES else DEFAULT_ROLE


def active_role() -> str:
    with _active_lock:
        return _active["role"]


def active_policy() -> dict:
    return POLICIES[active_role()]


def is_denied_all() -> bool:
    """True when the active principal has no usable policy (deny-all floor)."""
    return bool(active_policy().get("deny_all", False))


def visible_tables() -> frozenset[str] | None:
    """Bare table names the active policy exposes, for the context layer to
    show the model only what it can actually query (L0 map + search_schema).

    None means "unrestricted — show everything" (the admin / allow-raw-paths
    role with no governed tables). A restricted role returns exactly the names
    in its policy; the deny-all floor returns the empty set. This mirrors the
    rewrite's table-level deny-by-default so the model never sees, in its
    routing context, a table a query would then be refused for."""
    policy = active_policy()
    if policy.get("deny_all", False):
        return frozenset()
    if policy.get("allow_raw_paths", False) and not policy.get("tables"):
        return None
    return frozenset(policy.get("tables", {}))


def denied_columns() -> dict[str, frozenset[str]]:
    """Per-table CLS-denied columns for the active role, so schema discovery
    (search_schema) can hide the denied column NAMES too — the model is shown
    only what it can query. Empty for the unrestricted role. This is the same
    information a clean rejection would reveal, withheld from discovery so the
    model does not reach for a column it cannot select."""
    out: dict[str, frozenset[str]] = {}
    for name, rule in (active_policy().get("tables") or {}).items():
        cols = rule.get("deny_columns")
        if cols:
            out[name] = frozenset(c.lower() for c in cols)
    return out
