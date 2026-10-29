#!/usr/bin/env python3
"""Converge an account to `infra/lakeformation.yaml` — Lake Formation as the policy authority.

Everything the Lake Formation setup needs, as one idempotent command. Dry-run by
default: it prints what it would change. `--apply` changes it.

    ./.venv/bin/python scripts/setup_lakeformation.py                 # plan
    ./.venv/bin/python scripts/setup_lakeformation.py --apply         # converge
    ./.venv/bin/python scripts/setup_lakeformation.py --print-env     # LF_GOVERNANCE only

Steps, in the order that keeps the live agent working throughout:

  1. account settings: external-engine filtering ON, this account on the
     external-filtering allowlist, the session tag authorized (merged into
     the current settings — admins and other values are never removed);
  2. one IAM data access role per persona: trusted by the agent's execution
     role (and any --extra-trusted-principal) ONLY with the
     `LakeFormationAuthorizedCaller=<tag>` session tag, permitted to read the
     catalog metadata and — scoped to the table bucket — the S3 Tables
     federation layer, which authorizes AS THE CALLER;
  3. the execution role may assume exactly those persona roles;
  4. per governed table: full-table SELECT + DESCRIBE for the engine
     principals (execution role, LF admin), THEN revoke IAM_ALLOWED_PRINCIPALS
     — Lake Formation evaluates the union of grants, so leaving the
     pass-through grant in place silently swallows every filter;
  5. the data cells filters (created, or updated when the row filter or the
     excluded columns differ);
  6. SELECT on its filter (and DESCRIBE on the database) for each persona role;
  7. prints LF_GOVERNANCE, the env value the runtime reads.

The caller must be a Lake Formation data lake administrator. Nothing here
grants the AGENT more than it had: the execution role's physical reads are
unchanged; Lake Formation becomes the place the policy is written.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "infra" / "lakeformation.yaml"
TAG_KEY = "LakeFormationAuthorizedCaller"
READ_POLICY = "lf-policy-read"
FETCH_POLICY = "lf-policy-fetch"
_IDENT = re.compile(r"^[A-Za-z0-9_.-]+$")


# --------------------------------------------------------------- pure planning
def load_config(path: Path, account: str) -> dict:
    """Read and validate the declarative file; fail-stop on anything malformed."""
    raw = path.read_text(encoding="utf-8").replace("{account}", account)
    cfg = yaml.safe_load(raw)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path.name}: must be a mapping")
    tag = cfg.get("session_tag")
    if not isinstance(tag, str) or not _IDENT.match(tag):
        raise ValueError(f"{path.name}: session_tag must be a plain identifier")
    tables = cfg.get("tables") or {}
    personas = cfg.get("personas") or {}
    if not tables or not personas:
        raise ValueError(f"{path.name}: needs at least one table and one persona")
    for name, t in tables.items():
        missing = {"catalog_id", "database", "name", "table_bucket"} - set(t or {})
        if missing:
            raise ValueError(f"{path.name}: table {name!r} missing {sorted(missing)}")
    for persona, p in personas.items():
        if ":" not in persona:
            raise ValueError(f"{path.name}: persona {persona!r} must be tenant:role")
        if not _IDENT.match(str((p or {}).get("role_name", ""))):
            raise ValueError(f"{path.name}: persona {persona!r} needs a plain role_name")
        for table, f in ((p or {}).get("filters") or {}).items():
            if table not in tables:
                raise ValueError(f"{path.name}: persona {persona!r} filters unknown {table!r}")
            if not _IDENT.match(str((f or {}).get("name", ""))):
                raise ValueError(f"{path.name}: {persona} / {table}: filter needs a plain name")
            if not isinstance(f.get("row_filter"), str) or not f["row_filter"].strip():
                raise ValueError(f"{path.name}: {persona} / {table}: row_filter required")
            if not isinstance(f.get("exclude_columns", []), list):
                raise ValueError(f"{path.name}: {persona} / {table}: exclude_columns is a list")
    return cfg


def merge_settings(current: dict, account: str, tag: str) -> tuple[dict, list[str]]:
    """The account's DataLakeSettings with external filtering for this
    account and tag switched on. Returns (new settings, what changed); only
    ever ADDS — admins, defaults and other allowlist entries are kept."""
    new = copy.deepcopy(current)
    changes = []
    if not new.get("AllowExternalDataFiltering"):
        new["AllowExternalDataFiltering"] = True
        changes.append("AllowExternalDataFiltering → true")
    allow = new.setdefault("ExternalDataFilteringAllowList", [])
    if not any(e.get("DataLakePrincipalIdentifier") == account for e in allow):
        allow.append({"DataLakePrincipalIdentifier": account})
        changes.append(f"ExternalDataFilteringAllowList += {account}")
    tags = new.setdefault("AuthorizedSessionTagValueList", [])
    if tag not in tags:
        tags.append(tag)
        changes.append(f"AuthorizedSessionTagValueList += {tag}")
    return new, changes


def trust_policy(callers: list[str], tag: str) -> dict:
    """Who may assume a persona role: the named callers, and only WITH the tag."""
    principals = sorted(set(callers))
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": principals}, "Action": "sts:AssumeRole"},
            {
                "Effect": "Allow",
                "Principal": {"AWS": principals},
                "Action": "sts:TagSession",
                "Condition": {"StringEquals": {f"aws:RequestTag/{TAG_KEY}": tag}},
            },
        ],
    }


def existing_principals(trust_doc: dict | None) -> list[str]:
    """The AWS principals an existing trust policy lets assume the role."""
    out: list[str] = []
    for st in (trust_doc or {}).get("Statement", []):
        aws = (st.get("Principal") or {}).get("AWS", [])
        out += [aws] if isinstance(aws, str) else list(aws)
    return sorted(set(out))


def merge_statements(have: dict | None, want: dict, prune: bool) -> dict:
    """`want`, plus — unless pruning — every statement of `have` whose Sid
    `want` does not define. Converging must not silently take away a
    permission someone added for a reason (measured 2026-10-29: a run without
    the Athena flag stripped the idempotence check's grants)."""
    if prune or not have:
        return want
    sids = {st.get("Sid") for st in want["Statement"]}
    extra = [st for st in have.get("Statement", []) if st.get("Sid") not in sids]
    return {**want, "Statement": [*want["Statement"], *extra]}


def merge_resources(have: dict | None, want: dict, prune: bool) -> dict:
    """For the one-statement fetch policy: union the Resource lists unless pruning."""
    if prune or not have:
        return want
    old = []
    for st in have.get("Statement", []):
        r = st.get("Resource", [])
        old += [r] if isinstance(r, str) else list(r)
    st = dict(want["Statement"][0])
    st["Resource"] = sorted(set(st["Resource"]) | set(old))
    return {**want, "Statement": [st]}


def read_policy(region: str, account: str, buckets: list[str], athena_prefix: str | None) -> dict:
    """What a persona role may do: fetch its own LF view of the catalog, and
    pass the S3 Tables federation layer's caller-side checks — nothing else.
    `athena_prefix` (optional) adds what the idempotence check needs to run
    the same question on Athena as the same principal."""
    s3t = []
    for b in sorted(set(buckets)):
        arn = f"arn:aws:s3tables:{region}:{account}:bucket/{b}"
        s3t += [arn, f"{arn}/*"]
    statements = [
        {
            "Sid": "LFPolicyRead",
            "Effect": "Allow",
            "Action": [
                "lakeformation:GetDataAccess",
                "glue:GetTable",
                "glue:GetTables",
                "glue:GetDatabase",
                "glue:GetDatabases",
                "glue:GetCatalog",
                "glue:GetCatalogs",
                "glue:GetPartition",
                "glue:GetPartitions",
            ],
            "Resource": "*",
        },
        {
            "Sid": "S3TablesFederationRead",
            "Effect": "Allow",
            "Action": [
                "s3tables:GetTableBucket",
                "s3tables:GetNamespace",
                "s3tables:ListNamespaces",
                "s3tables:GetTable",
                "s3tables:ListTables",
                "s3tables:GetTableMetadataLocation",
                "s3tables:GetTableData",
            ],
            "Resource": s3t,
        },
    ]
    if athena_prefix:
        m = re.match(r"^s3://([^/]+)/(.+?)/?$", athena_prefix)
        if not m:
            raise ValueError("--athena-results must be s3://bucket/prefix/")
        bucket, prefix = m.groups()
        statements += [
            {
                "Sid": "AthenaIdempotenceCheck",
                "Effect": "Allow",
                "Action": [
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                ],
                "Resource": f"arn:aws:athena:{region}:{account}:workgroup/primary",
            },
            {
                "Sid": "AthenaResults",
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:GetBucketLocation",
                    "s3:ListBucket",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/{prefix}/*"],
            },
        ]
    return {"Version": "2012-10-17", "Statement": statements}


def filter_payload(table: dict, f: dict) -> dict:
    """The DataCellsFilter body for one persona × table."""
    return {
        "TableCatalogId": table["catalog_id"],
        "DatabaseName": table["database"],
        "TableName": table["name"],
        "Name": f["name"],
        "RowFilter": {"FilterExpression": f["row_filter"]},
        "ColumnWildcard": {"ExcludedColumnNames": list(f.get("exclude_columns") or [])},
    }


def filter_differs(live: dict, wanted: dict) -> bool:
    lrow = ((live.get("RowFilter") or {}).get("FilterExpression") or "").strip()
    lexc = sorted((live.get("ColumnWildcard") or {}).get("ExcludedColumnNames") or [])
    wexc = sorted(wanted["ColumnWildcard"]["ExcludedColumnNames"])
    return lrow != wanted["RowFilter"]["FilterExpression"].strip() or lexc != wexc


def lf_governance(cfg: dict, account: str) -> dict:
    """The LF_GOVERNANCE env value (governance/lakeformation.py's schema)."""
    return {
        "session_tag": cfg["session_tag"],
        "roles": {
            persona: f"arn:aws:iam::{account}:role/{p['role_name']}"
            for persona, p in sorted(cfg["personas"].items())
        },
        "tables": {
            name: {"catalog_id": t["catalog_id"], "database": t["database"], "name": t["name"]}
            for name, t in sorted(cfg["tables"].items())
        },
    }


# ------------------------------------------------------------------ AWS steps
def _retry_new_principal(fn, attempts: int = 12, delay: float = 5.0):
    """Lake Formation rejects an IAM role created seconds ago ("Invalid
    principal") until IAM has propagated it; retry that one error only."""
    import time

    from botocore.exceptions import ClientError

    for i in range(attempts):
        try:
            return fn()
        except ClientError as e:
            msg = e.response["Error"].get("Message", "")
            if "Invalid principal" not in msg or i == attempts - 1:
                raise
            time.sleep(delay)  # nosemgrep: arbitrary-sleep -- IAM propagation retry
    return None


def _grant(lf, principal: str, resource: dict, perms: list[str]):
    return _retry_new_principal(
        lambda: lf.grant_permissions(
            Principal={"DataLakePrincipalIdentifier": principal},
            Resource=resource,
            Permissions=perms,
        )
    )


class Plan:
    def __init__(self, apply: bool) -> None:
        self.apply = apply
        self.changes = 0

    def step(self, text: str, fn=None):
        self.changes += 1
        print(f"  {'✓' if self.apply else '·'} {text}")
        if self.apply and fn is not None:
            return fn()
        return None


def _caller_role_arn(sts, iam) -> str:
    arn = sts.get_caller_identity()["Arn"]
    m = re.match(r"^arn:aws:sts::(\d+):assumed-role/([^/]+)/", arn)
    return f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}" if m else arn


def _runtime_role(ctl, name: str) -> str:
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for r in page.get("agentRuntimes", []):
            if r["agentRuntimeName"] == name:
                return ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])["roleArn"]
    sys.exit(f"no agent runtime named {name!r} (pass --execution-role-arn)")


def _norm(doc):
    """IAM returns policy documents with its own list order and collapses a
    one-element list to a scalar; compare meaning, not formatting."""
    if isinstance(doc, dict):
        return {k: _norm(v) for k, v in doc.items()}
    if isinstance(doc, list):
        items = [_norm(x) for x in doc]
        if len(items) == 1 and not isinstance(items[0], (dict, list)):
            return items[0]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    return doc


def _same_doc(a, b) -> bool:
    return a is not None and _norm(a) == _norm(b)


def converge(args) -> int:
    import boto3
    from botocore.exceptions import ClientError

    region = args.region
    sts = boto3.client("sts", region_name=region)
    iam = boto3.client("iam")
    lf = boto3.client("lakeformation", region_name=region)
    account = sts.get_caller_identity()["Account"]
    cfg = load_config(Path(args.config), account)
    if args.print_env:
        print(json.dumps(lf_governance(cfg, account), separators=(",", ":")))
        return 0
    exec_role = args.execution_role_arn or _runtime_role(
        boto3.client("bedrock-agentcore-control", region_name=region), args.runtime_name
    )
    admin = args.admin_principal or _caller_role_arn(sts, iam)
    callers = [exec_role, admin, *args.extra_trusted_principal]
    plan = Plan(args.apply)
    print(f"account {account} · execution role {exec_role.rsplit('/', 1)[-1]} · admin {admin}")

    # 1. account settings
    print("1. account settings")
    cur = lf.get_data_lake_settings()["DataLakeSettings"]
    new, changes = merge_settings(cur, account, cfg["session_tag"])
    for c in changes:
        plan.step(c)
    if changes and args.apply:
        lf.put_data_lake_settings(DataLakeSettings=new)

    # 2. persona roles
    print("2. persona data access roles")
    buckets = [t["table_bucket"] for t in cfg["tables"].values()]
    want_read = read_policy(region, account, buckets, args.athena_results)
    persona_arns = []
    fresh_roles: set[str] = set()  # created this run, or absent in a dry-run
    for persona, p in sorted(cfg["personas"].items()):
        name = p["role_name"]
        persona_arns.append(f"arn:aws:iam::{account}:role/{name}")
        try:
            role = iam.get_role(RoleName=name)["Role"]
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
            role = None
        trusted = callers if (args.prune or role is None) else [
            *callers, *existing_principals(role["AssumeRolePolicyDocument"])
        ]
        want_trust = trust_policy(trusted, cfg["session_tag"])
        if role is None:
            fresh_roles.add(f"arn:aws:iam::{account}:role/{name}")
            plan.step(
                f"create role {name} ({persona})",
                lambda n=name, per=persona, tr=want_trust: iam.create_role(
                    RoleName=n,
                    AssumeRolePolicyDocument=json.dumps(tr),
                    Description=f"Lake Formation data access role for persona {per} (data agent)",
                    MaxSessionDuration=3600,
                ),
            )
        elif not _same_doc(role["AssumeRolePolicyDocument"], want_trust):
            plan.step(
                f"update trust of {name}",
                lambda n=name, tr=want_trust: iam.update_assume_role_policy(
                    RoleName=n, PolicyDocument=json.dumps(tr)
                ),
            )
        try:
            have = iam.get_role_policy(RoleName=name, PolicyName=READ_POLICY)["PolicyDocument"]
        except ClientError:
            have = None
        doc = merge_statements(have, want_read, args.prune)
        if not _same_doc(have, doc):
            plan.step(
                f"put {READ_POLICY} on {name}",
                lambda n=name, d=doc: iam.put_role_policy(
                    RoleName=n, PolicyName=READ_POLICY, PolicyDocument=json.dumps(d)
                ),
            )

    # 3. the execution role may assume exactly those roles
    print("3. execution role → persona roles")
    exec_name = exec_role.rsplit("/", 1)[-1]
    want_fetch = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeLFPersonaRoles",
                "Effect": "Allow",
                "Action": ["sts:AssumeRole", "sts:TagSession"],
                "Resource": sorted(persona_arns),
            }
        ],
    }
    try:
        have = iam.get_role_policy(RoleName=exec_name, PolicyName=FETCH_POLICY)["PolicyDocument"]
    except ClientError:
        have = None
    want_fetch = merge_resources(have, want_fetch, args.prune)
    if not _same_doc(have, want_fetch):
        plan.step(
            f"put {FETCH_POLICY} on {exec_name}",
            lambda: iam.put_role_policy(
                RoleName=exec_name, PolicyName=FETCH_POLICY, PolicyDocument=json.dumps(want_fetch)
            ),
        )

    # 4. engine principals get the full table, THEN the pass-through goes
    print("4. table grants (engine principals) and pass-through revoke")
    for lname, t in sorted(cfg["tables"].items()):
        res = {"Table": {"CatalogId": t["catalog_id"], "DatabaseName": t["database"],
                         "Name": t["name"]}}  # fmt: skip
        for principal in sorted({exec_role, admin}):
            got = lf.list_permissions(
                Principal={"DataLakePrincipalIdentifier": principal}, Resource=res
            )["PrincipalResourcePermissions"]
            perms = {x for g in got for x in g["Permissions"]}
            if not {"SELECT", "DESCRIBE"} <= perms and "ALL" not in perms:
                plan.step(
                    f"grant SELECT+DESCRIBE on {lname} to {principal.rsplit('/', 1)[-1]}",
                    lambda pr=principal, r=res: _grant(lf, pr, r, ["SELECT", "DESCRIBE"]),
                )
        passthru = [
            g
            for g in lf.list_permissions(Resource=res)["PrincipalResourcePermissions"]
            if g["Principal"]["DataLakePrincipalIdentifier"] == "IAM_ALLOWED_PRINCIPALS"
        ]
        if passthru:
            plan.step(
                f"revoke IAM_ALLOWED_PRINCIPALS on {lname} (otherwise the filters are swallowed)",
                lambda r=res, g=passthru: lf.revoke_permissions(
                    Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
                    Resource=r,
                    Permissions=sorted({x for p in g for x in p["Permissions"]}),
                ),
            )

    # 5 + 6. filters and the persona grants on them
    print("5. data cells filters  ·  6. persona grants")
    for _persona, p in sorted(cfg["personas"].items()):
        role_arn = f"arn:aws:iam::{account}:role/{p['role_name']}"
        for lname, f in sorted((p.get("filters") or {}).items()):
            t = cfg["tables"][lname]
            body = filter_payload(t, f)
            try:
                live = lf.get_data_cells_filter(
                    TableCatalogId=t["catalog_id"],
                    DatabaseName=t["database"],
                    TableName=t["name"],
                    Name=f["name"],
                )["DataCellsFilter"]
            except ClientError as e:
                if e.response["Error"]["Code"] != "EntityNotFoundException":
                    raise
                live = None
            if live is None:
                plan.step(f"create filter {f['name']} on {lname}",
                          lambda b=body: lf.create_data_cells_filter(TableData=b))  # fmt: skip
            elif filter_differs(live, body):
                plan.step(f"update filter {f['name']} on {lname}",
                          lambda b=body: lf.update_data_cells_filter(TableData=b))  # fmt: skip
            fres = {"DataCellsFilter": {"TableCatalogId": t["catalog_id"],
                    "DatabaseName": t["database"], "TableName": t["name"],
                    "Name": f["name"]}}  # fmt: skip
            fresh = role_arn in fresh_roles
            got = [] if (live is None or fresh) else lf.list_permissions(
                Principal={"DataLakePrincipalIdentifier": role_arn}, Resource=fres
            )["PrincipalResourcePermissions"]  # fmt: skip
            if not any("SELECT" in g["Permissions"] for g in got):
                plan.step(
                    f"grant SELECT on filter {f['name']} to {p['role_name']}",
                    lambda r=fres, pr=role_arn: _grant(lf, pr, r, ["SELECT"]),
                )
            dres = {"Database": {"CatalogId": t["catalog_id"], "Name": t["database"]}}
            got = [] if fresh else lf.list_permissions(
                Principal={"DataLakePrincipalIdentifier": role_arn}, Resource=dres
            )["PrincipalResourcePermissions"]  # fmt: skip
            if not any({"DESCRIBE", "ALL"} & set(g["Permissions"]) for g in got):
                plan.step(
                    f"grant DESCRIBE on database {t['database']} to {p['role_name']}",
                    lambda r=dres, pr=role_arn: _grant(lf, pr, r, ["DESCRIBE"]),
                )

    print(f"\n{plan.changes} change(s) {'applied' if args.apply else 'planned (dry-run)'}")
    print("\nLF_GOVERNANCE=" + json.dumps(lf_governance(cfg, account), separators=(",", ":")))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--region", default=os.environ.get("AGENT_REGION", "us-west-2"))
    ap.add_argument("--runtime-name", default="duckdb_analyst")
    ap.add_argument("--execution-role-arn", default="")
    ap.add_argument("--admin-principal", default="", help="default: the calling role")
    ap.add_argument("--extra-trusted-principal", action="append", default=[])
    ap.add_argument("--athena-results", default="", help="s3://bucket/prefix/ (idempotence)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--prune",
        action="store_true",
        help="also REMOVE trusted principals / policy statements this file does not name "
        "(default: converge additively, never take a permission away silently)",
    )
    ap.add_argument("--print-env", action="store_true")
    return converge(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
