"""Lake Formation as the policy authority.

Row/column policy for the governed lake tables is AUTHORED in Lake Formation
(console/API — the same filters Athena, Redshift and EMR enforce) and fetched
here at session bind through the standard external-engine handshake (see
`docs/design.md`, "Lake Formation as the policy authority"):

    claims → persona data access role
      → sts:AssumeRole + sts:TagSession (LakeFormationAuthorizedCaller=<tag>)
      → glue:GetUnfilteredTableMetadata AS that principal, per governed table
      → AuthorizedColumns + CellFilters[].RowFilterExpression (PartiQL)
      → compiled into the session policy the rewrite layer already enforces

The ENV policy stays as the merged LOWER BOUND: row filters are
AND-ed, denied columns are unioned — the stricter of the two always wins. The
data path does not move: the engine keeps reading S3 Tables through the
execution role; what Lake Formation changes is where policy lives.

Fail-closed, table by table: any failure — the assume, the metadata call, a
cell-level filter the rewrite cannot express as one predicate, an expression
sqlglot cannot parse — REMOVES that table from the session policy, which is
the existing table-level deny (the model cannot see or query it). A fetch
failure never falls back to the env rule alone: a deployment that names a
Lake Formation role for a persona has declared LF authoritative for it.

Configuration is one env var, `LF_GOVERNANCE` (absent = disabled, env policy
is the sole authority — the no-LF deployment tier):

    {"session_tag": "data-agent",
     "roles":  {"<tenant>:<role>": "arn:aws:iam::…:role/…", …},
     "tables": {"<logical name>": {"catalog_id": "…", "database": "…",
                "name": "…"}, …}}

A persona not named in `roles` is env-governed (its tables may not be in a
Glue catalog at all); a mapped table outside the persona's policy stays
denied by default exactly as before.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time

import sqlglot
from sqlglot import exp

from . import policies

_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")
_TAG_KEY = "LakeFormationAuthorizedCaller"
TTL_S = int(os.environ.get("LF_POLICY_TTL_S", "900"))
_FAIL_TTL_S = 60  # a failed fetch is retried soon, but not hammered

_cache: dict[str, tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()


class LFConfigError(ValueError):
    """Malformed LF_GOVERNANCE (operator error, fail-stop at import)."""


def _load_config() -> dict | None:
    raw = os.environ.get("LF_GOVERNANCE")
    if not raw:
        return None
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LFConfigError(f"governance: LF_GOVERNANCE does not parse ({e})") from e
    if not isinstance(cfg, dict):
        raise LFConfigError("governance: LF_GOVERNANCE must be a JSON object")
    unknown = set(cfg) - {"session_tag", "roles", "tables"}
    if unknown:
        raise LFConfigError(f"governance: LF_GOVERNANCE unknown keys {sorted(unknown)}")
    tag = cfg.get("session_tag")
    if not isinstance(tag, str) or not tag.strip():
        raise LFConfigError("governance: LF_GOVERNANCE.session_tag must be a non-empty string")
    roles = cfg.get("roles") or {}
    if not isinstance(roles, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _ARN_RE.match(v) for k, v in roles.items()
    ):
        raise LFConfigError("governance: LF_GOVERNANCE.roles must map persona → IAM role ARN")
    tables = cfg.get("tables") or {}
    if not isinstance(tables, dict):
        raise LFConfigError("governance: LF_GOVERNANCE.tables must be an object")
    for lname, t in tables.items():
        if (
            not isinstance(t, dict)
            or set(t) != {"catalog_id", "database", "name"}
            or not all(isinstance(t[k], str) and t[k] for k in ("catalog_id", "database", "name"))
        ):
            raise LFConfigError(
                f"governance: LF_GOVERNANCE.tables[{lname!r}] needs catalog_id/database/name"
            )
    return cfg


CONFIG = _load_config()


def enabled_for(role: str) -> bool:
    return bool(CONFIG and (CONFIG.get("roles") or {}).get(role))


def _assumed_clients(role_arn: str, tag: str):
    """Glue client under the persona's data access role, tagged for LF."""
    import boto3  # the runtime ships it; tests never reach this line

    region = os.environ.get("AWS_REGION", "us-west-2")
    sts = boto3.client("sts", region_name=region)
    cred = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="data-agent-lf-policy",
        Tags=[{"Key": _TAG_KEY, "Value": tag}],
        DurationSeconds=900,
    )["Credentials"]
    return boto3.client(
        "glue",
        region_name=region,
        aws_access_key_id=cred["AccessKeyId"],
        aws_secret_access_key=cred["SecretAccessKey"],
        aws_session_token=cred["SessionToken"],
    )


def _fetch_table_rule(glue, locus: dict) -> dict:
    """One table's LF view for the assumed principal → a policy rule.

    Returns {"row_filter": str|None, "deny_columns": [..]}. Raises on anything
    the rewrite cannot faithfully express — the caller turns that into a
    table-level deny (fail-closed), never a weaker rule.
    """
    # the spike-verified quirk: pass exactly ONE supported permission type
    out = glue.get_unfiltered_table_metadata(
        CatalogId=locus["catalog_id"],
        DatabaseName=locus["database"],
        Name=locus["name"],
        SupportedPermissionTypes=["CELL_FILTER_PERMISSION"],
    )
    all_cols = [c["Name"] for c in out["Table"]["StorageDescriptor"]["Columns"]]
    authorized = out.get("AuthorizedColumns")
    if not authorized:
        raise ValueError("LF returned no authorized columns")
    deny = [c for c in all_cols if c not in set(authorized)]
    exprs = {
        (f.get("RowFilterExpression") or "").strip()
        for f in out.get("CellFilters") or []
        if (f.get("RowFilterExpression") or "").strip()
    }
    exprs -= {"TRUE"}
    if len(exprs) > 1:
        # different predicates on different columns = cell-level security,
        # which a single WHERE predicate cannot express — fail closed
        raise ValueError(
            f"cell-level filters are not expressible as one predicate: {sorted(exprs)}"
        )
    row_filter = next(iter(exprs), None)
    if row_filter is not None:
        probe = sqlglot.parse_one(  # nosec B608 - validation-only parse
            f"SELECT * FROM t WHERE {row_filter}",  # nosec B608 - parsed, never executed
            read="duckdb",
        )
        if not isinstance(probe, exp.Select) or probe.args.get("where") is None:
            raise ValueError(f"LF row filter is not a WHERE predicate: {row_filter!r}")
    return {"row_filter": row_filter, "deny_columns": deny}


def _merge_rule(env_rule: dict, lf_rule: dict) -> dict:
    """Stricter-wins merge: AND the row filters, union the denied columns."""
    merged: dict = {}
    env_f, lf_f = env_rule.get("row_filter"), lf_rule.get("row_filter")
    if env_f and lf_f:
        merged["row_filter"] = f"({env_f}) AND ({lf_f})" if env_f != lf_f else env_f
    elif env_f or lf_f:
        merged["row_filter"] = env_f or lf_f
    deny = list(
        dict.fromkeys((env_rule.get("deny_columns") or []) + (lf_rule.get("deny_columns") or []))
    )
    if deny:
        merged["deny_columns"] = deny
    return merged


def build_session_policy(role: str) -> tuple[dict | None, list[str]]:
    """The merged session policy for `role`, plus human-readable notes.

    (None, notes) means "no overlay" — LF is not configured for this persona.
    Networked; call it off the event loop. Fail-closed per table: a mapped
    table whose fetch fails is REMOVED from the returned policy.
    """
    if not enabled_for(role) or role not in policies.POLICIES:
        return None, []
    base = copy.deepcopy(policies.POLICIES[role])
    if base.get("deny_all") or (base.get("allow_raw_paths") and not base.get("tables")):
        return None, []  # the floor and the unrestricted role take no overlay
    tables = base.get("tables") or {}
    mapped = {n: loc for n, loc in (CONFIG.get("tables") or {}).items() if n in tables}
    if not mapped:
        return None, []
    notes: list[str] = []
    try:
        glue = _assumed_clients(CONFIG["roles"][role], CONFIG["session_tag"])
    except Exception as e:  # noqa: BLE001 — no principal, no LF view: deny every mapped table
        for name in mapped:
            tables.pop(name, None)
            notes.append(f"{name}: DENIED (assume failed: {str(e)[:120]})")
        return base, notes
    for name, locus in sorted(mapped.items()):
        try:
            lf_rule = _fetch_table_rule(glue, locus)
        except Exception as e:  # noqa: BLE001 — fail closed, never fall back to env alone
            tables.pop(name, None)
            notes.append(f"{name}: DENIED ({str(e)[:160]})")
            continue
        tables[name] = _merge_rule(tables.get(name) or {}, lf_rule)
        notes.append(
            f"{name}: rls={'yes' if lf_rule['row_filter'] else 'no'}"
            f" cls={len(lf_rule['deny_columns'])}"
        )
    return base, notes


def bind(role: str | None = None) -> list[str]:
    """Fetch-and-install the LF session policy for the active role (cached).

    Called at session bind on both inbound surfaces. Returns the notes (empty
    when LF is not in play for this persona). Never raises — a failure is
    already encoded in the policy as table-level denies.
    """
    role = role or policies.active_role()
    if not enabled_for(role):
        return []
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(role)
        if hit and hit[0] > now:
            policies.set_session_policy(copy.deepcopy(hit[1]) if hit[1] else None)
            return []
    overlay, notes = build_session_policy(role)
    failed = any("DENIED" in n for n in notes)
    with _cache_lock:
        _cache[role] = (now + (_FAIL_TTL_S if failed else TTL_S), overlay)
    policies.set_session_policy(copy.deepcopy(overlay) if overlay else None)
    return notes


def reset() -> None:
    """Forget the cache (tests, config swaps)."""
    with _cache_lock:
        _cache.clear()
