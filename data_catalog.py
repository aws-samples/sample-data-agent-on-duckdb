"""Data catalog for the on-chain fund-flow investigation scenario.

Public dataset: s3://aws-public-blockchain (region us-east-2, public read)
Layout: v1.0/eth/{blocks,transactions,logs,token_transfers,traces,contracts}/
        date=YYYY-MM-DD/*.parquet

The scenario is Ethereum-native on-chain investigation (fund-flow tracing +
sanctions-exposure screening). The star table is `eth_traces` — internal
calls, the only place a contract-mediated drain (which never appears in the
top-level `transactions` table) is visible.

All public tables are hive-partitioned by `date` (string, YYYY-MM-DD).
Partition pruning only works when queries filter on `date`, so the agent is
instructed to ALWAYS include a date filter.

Two small label tables give the raw ledger the attribution the analyst needs
(the public dataset has addresses and amounts but no entity labels):
  - `ofac_addresses`     — REAL public OFAC-sanctioned ETH addresses
  - `cex_deposit_labels` — SYNTHETIC exchange deposit-address labels
They are exposed as views over Parquet on the solution's own bucket, gated by
the LABELS_PREFIX env var (empty = off, so raw-only runs still work).
"""

S3_BASE = "s3://aws-public-blockchain/v1.0"


# Views are defined lazily as read_parquet over the hive layout. Globbing the
# whole prefix costs an S3 LIST of thousands of partitions, so single-day
# questions should use an explicit path instead (the agent prompt covers this).
def _view(chain: str, table: str) -> str:
    return f"read_parquet('{S3_BASE}/{chain}/{table}/*/*.parquet', hive_partitioning=1)"


VIEW_DEFS = {
    "eth_blocks": _view("eth", "blocks"),
    "eth_transactions": _view("eth", "transactions"),
    "eth_token_transfers": _view("eth", "token_transfers"),
    "eth_traces": _view("eth", "traces"),
    "eth_logs": _view("eth", "logs"),
}

# Label views resolve to single Parquet files on the solution bucket (tiny —
# no hive layout, no partitioning). Built only when LABELS_PREFIX is set.
LABEL_TABLES = ("ofac_addresses", "cex_deposit_labels")


def build_label_views(prefix: str) -> dict[str, str]:
    """View defs for the label tables under an s3:// prefix (or local dir)."""
    if not prefix:
        return {}
    base = prefix.rstrip("/")
    return {name: f"read_parquet('{base}/{name}.parquet')" for name in LABEL_TABLES}


# The catalog-leg slice (all non-raw legs) shares this shape: the same
# incident-window `eth_traces` slice (internal calls with value > 0), loaded
# once from the raw Parquet. Address columns are what make the governance and
# tracing story meaningful.
SLICE_COLUMNS = (
    "block_number, transaction_hash, from_address, to_address, value (DOUBLE, wei), "
    "call_type, trace_type, status, block_timestamp, date (VARCHAR)"
)

_RAW_DOC = f"""
## Available data (AWS Public Blockchain dataset, Ethereum, updated daily)

Base path: {S3_BASE}/  — hive-partitioned by date=YYYY-MM-DD

| Table (view) | Path | Notes |
|---|---|---|
| eth_blocks | {S3_BASE}/eth/blocks/ | Ethereum blocks since 2015-07-30 |
| eth_transactions | {S3_BASE}/eth/transactions/ | Top-level txs; `value` wei; from/to_address |
| eth_token_transfers | {S3_BASE}/eth/token_transfers/ | ERC-20/721: token_address, from/to_address, value |
| eth_traces | {S3_BASE}/eth/traces/ | INTERNAL calls: from_address, to_address, value, call_type, trace_type, status — a contract-mediated transfer shows up ONLY here, never in eth_transactions |
| eth_logs | {S3_BASE}/eth/logs/ | Raw event logs: address, topics[], data |
"""  # noqa: E501 — markdown table rows in the system-prompt doc

_LABEL_DOC = """
## Attribution label tables (join the raw ledger to entities)

| Table (view) | Rows | Source | Columns |
|---|---|---|---|
| ofac_addresses | ~120 | REAL public OFAC SDN crypto list | address, program, source |
| cex_deposit_labels | ~12 | SYNTHETIC | address, exchange, label, subject_identity, risk_tier |

The public ledger has addresses and amounts but no identities; these tables
overlay attribution the way a commercial intelligence feed would. Join on
lower(address). `cex_deposit_labels` is synthetic sample data, not real
attribution.
"""

# One entry per optional catalog leg: (alias, heading, how it resolves a table
# name to data). The "when to use" table below is assembled from active legs.
_LEG_DOCS = {
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

_SCENARIO_ROWS = {
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

_QUERY_RULES = f"""
### Query rules (IMPORTANT for cost & latency)
1. ALWAYS filter on `date` (string 'YYYY-MM-DD'). Full scans over all raw
   history list thousands of partitions and read TBs — never do that.
2. For a SINGLE day on raw data, prefer an explicit path — avoids the S3 LIST:
   read_parquet('{S3_BASE}/eth/traces/date=2025-02-21/*.parquet')
3. For a date RANGE, use the pre-defined view + WHERE date BETWEEN ... (partition
   pruning applies after listing).
4. Start with DESCRIBE or LIMIT 5 to learn the schema before aggregating.
5. ETH `value` is in wei — divide by 1e18 for ETH. A single day of eth_traces
   is a few GB; ALWAYS filter by from_address/to_address (or a date path) so the
   scan collapses to the rows of the case, then CREATE TEMP TABLE the result and
   interrogate that in-memory working set instead of re-scanning S3.
"""


def build_catalog_doc(legs: list[str], labels: bool = False) -> str:
    """Assemble the system-prompt catalog doc for the active legs.

    `legs` is the list of attached optional catalogs ('s3t', 'glue', 'dl');
    the raw-Parquet views are always present. `labels` adds the attribution
    label-table section when the label views are mounted.
    """
    parts = [_RAW_DOC]
    if labels:
        parts.append(_LABEL_DOC)
    if legs:
        parts.append(
            "\n## Catalog-based access methods — same data, different resolution\n\n"
            "All catalogs below hold the same incident-window `eth_traces` slice"
            f" (internal calls with value > 0) with columns: {SLICE_COLUMNS}.\n"
            "Prefer a catalog when its slice covers the question (no S3 LIST);"
            " prefer raw views for data outside the loaded window.\n"
        )
        for leg in legs:
            heading, body = _LEG_DOCS[leg]
            parts.append(f"\n{heading}\n{body}\n")
        parts.append(
            "\n### Which access path to use\n\n"
            "| Access path | Resolution | Best for |\n|---|---|---|\n"
            + "\n".join(_SCENARIO_ROWS[k] for k in ["raw", *legs])
            + "\n"
        )
    parts.append(_QUERY_RULES)
    return "".join(parts)


# default doc (raw only) — kept for import compatibility and tests
CATALOG_DOC = build_catalog_doc([])
