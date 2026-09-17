"""Catalog legs: the optional access paths to the same data on S3.

The raw-Parquet views are always on. Three catalog legs mount READ_ONLY,
each toggled by its own env var (empty = off), each under a fixed ≤4-char
alias so `s3t.blockchain.eth_traces`, `glue.…` and `dl.…` read naturally:

  s3t   S3 Tables native Iceberg REST catalog        S3_TABLES_ARN
  glue  Glue Data Catalog Iceberg REST (federated)   GLUE_CATALOG
  dl    DuckLake SQL-database catalog                DUCKLAKE_CATALOG

Only ACTIVE legs are documented in the prompt — never let an inactive leg
leak into what the model is told it can query.
"""

from __future__ import annotations

import json
import os

import duckdb

S3_TABLES_ARN = os.environ.get("S3_TABLES_ARN", "")  # s3t: native Iceberg REST
GLUE_CATALOG = os.environ.get("GLUE_CATALOG", "")  # glue: '<acct>:s3tablescatalog/<bucket>'
# dl: DuckLake catalog. Two forms:
#   's3://.../catalog.ducklake'        — DuckDB-file catalog (single-writer)
#   'postgres:dbname=...'              — RDS PostgreSQL catalog (multi-client);
#     connection credentials come from Secrets Manager (DUCKLAKE_PG_SECRET)
DUCKLAKE_CATALOG = os.environ.get("DUCKLAKE_CATALOG", "")
DUCKLAKE_PG_SECRET = os.environ.get("DUCKLAKE_PG_SECRET", "duckdb-agent/ducklake-pg")


def active_legs() -> list[str]:
    """Optional catalog legs enabled by env, in mount order."""
    legs = []
    if S3_TABLES_ARN:
        legs.append("s3t")
    if GLUE_CATALOG:
        legs.append("glue")
    if DUCKLAKE_CATALOG:
        legs.append("dl")
    return legs


def attach(c: duckdb.DuckDBPyConnection) -> None:
    """Mount each enabled catalog leg READ_ONLY under its fixed alias. The
    extensions are pre-installed in the image: LOAD only, so a
    cold start never reaches the extension repository."""
    from data_agent.compute.engine import load_extension  # LOAD-only helper

    if S3_TABLES_ARN or GLUE_CATALOG:
        load_extension(c, "iceberg")
    if S3_TABLES_ARN:  # Iceberg REST, S3 Tables native endpoint
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config, not user input
            f"ATTACH '{S3_TABLES_ARN}' AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY)"
        )
    if GLUE_CATALOG:  # Iceberg REST, Glue endpoint (federated s3tablescatalog)
        c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
            f"ATTACH '{GLUE_CATALOG}' AS glue (TYPE iceberg, ENDPOINT_TYPE glue, READ_ONLY)"
        )
    if DUCKLAKE_CATALOG:  # SQL-database catalog; Parquet data on S3
        load_extension(c, "ducklake")
        if DUCKLAKE_CATALOG.startswith("postgres:"):
            # RDS PostgreSQL catalog: credentials from Secrets Manager, never env
            import boto3

            sec = json.loads(
                boto3.client("secretsmanager").get_secret_value(SecretId=DUCKLAKE_PG_SECRET)[
                    "SecretString"
                ]
            )
            load_extension(c, "postgres")  # not baked: the PG catalog form is not deployed
            c.execute(
                "CREATE SECRET pgcat (TYPE postgres, "
                f"HOST '{sec['host']}', PORT {sec.get('port', 5432)}, "
                f"DATABASE '{sec['dbname']}', "
                f"USER '{sec['username']}', PASSWORD '{sec['password']}')"
            )
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY, META_SECRET pgcat)"
            )
        else:
            c.execute(  # nosemgrep: sqlalchemy-execute-raw-query -- env config
                f"ATTACH 'ducklake:{DUCKLAKE_CATALOG}' AS dl (READ_ONLY)"
            )


# ---------------------------------------------------------------------- doc
# The catalog-leg slice (all non-raw legs) shares this shape: the same
# incident-window `eth_traces` slice (internal calls with value > 0), loaded
# once from the raw Parquet. Address columns are what make the governance and
# tracing story meaningful.
SLICE_COLUMNS = (
    "block_number, transaction_hash, from_address, to_address, value (DOUBLE, wei), "
    "call_type, trace_type, status, block_timestamp, date (VARCHAR)"
)

# One entry per optional catalog leg: (heading, how it resolves a table name
# to data). The "when to use" table below is assembled from active legs.
LEG_DOCS = {
    "s3t": (
        "### `s3t` — S3 Tables native Iceberg REST catalog",
        "`s3t.blockchain.eth_traces` — the incident-window internal-call slice. "
        "Resolution: one REST pointer lookup (S3 Tables service) + Iceberg "
        "manifest reads, zero S3 LIST. The catalog service owns the "
        "current-version pointer.",
    ),
    "glue": (
        "### `glue` — same Iceberg tables via AWS Glue Data Catalog (SageMaker Lakehouse)",
        "`glue.blockchain.eth_traces` — THE SAME physical table as "
        "`s3t.blockchain.eth_traces`, resolved through the Glue Iceberg REST "
        "endpoint (federated s3tablescatalog) instead of the S3 Tables native "
        "endpoint. Same data, one more governance hop (Lake Formation).",
    ),
    "dl": (
        "### `dl` — DuckLake (SQL-database catalog, Parquet data on S3)",
        "`dl.blockchain.eth_traces` — same incident-window slice. All metadata "
        "(snapshots, schema, file list) lives in ONE catalog database; "
        "resolution is a single SQL lookup, then direct Parquet range GETs — "
        "no metadata-file chain to walk.",
    ),
}

ACCESS_PATH_ROWS = {
    "raw": (
        "| raw views / read_parquet | Path glob, S3 LIST "
        "| Ad-hoc files, full history, no setup — data as it lands |"
    ),
    "s3t": (
        "| `s3t.*` | Iceberg REST (S3 Tables service) "
        "| Managed Iceberg: ACID, compaction, snapshots — engine-direct, IAM-only |"
    ),
    "glue": (
        "| `glue.*` | Iceberg REST (Glue/Lake Formation) "
        "| Same tables, org-wide catalog: share with Athena/Redshift, central governance |"
    ),
    "dl": (
        "| `dl.*` | SQL catalog database (DuckLake) "
        "| Metadata-heavy work: many small tables/snapshots, single-query resolution |"
    ),
}


def doc_section(legs: list[str]) -> str:
    """The prompt section for the ACTIVE legs ('s3t', 'glue', 'dl'); empty when none."""
    if not legs:
        return ""
    parts = [
        "\n## Catalog-based access methods — same data, different resolution\n\n"
        "All catalogs below hold the same incident-window `eth_traces` slice"
        f" (internal calls with value > 0) with columns: {SLICE_COLUMNS}.\n"
        "Prefer a catalog when its slice covers the question (no S3 LIST);"
        " prefer raw views for data outside the loaded window.\n"
    ]
    for leg in legs:
        heading, body = LEG_DOCS[leg]
        parts.append(f"\n{heading}\n{body}\n")
    parts.append(
        "\n### Which access path to use\n\n"
        "| Access path | Resolution | Best for |\n|---|---|---|\n"
        + "\n".join(ACCESS_PATH_ROWS[k] for k in ["raw", *legs])
        + "\n"
    )
    return "".join(parts)
