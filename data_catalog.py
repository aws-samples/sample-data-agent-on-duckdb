"""Data catalog for the AWS Public Blockchain dataset (Registry of Open Data).

Bucket: s3://aws-public-blockchain (region us-east-2, public read)
Layout: v1.0/btc/{blocks,transactions}/date=YYYY-MM-DD/*.parquet
        v1.0/eth/{blocks,transactions,logs,token_transfers,traces,contracts}/date=YYYY-MM-DD/*.parquet

All tables are hive-partitioned by `date` (string, YYYY-MM-DD). Partition pruning
only works when queries filter on `date`, so the agent is instructed to ALWAYS
include a date filter.
"""

S3_BASE = "s3://aws-public-blockchain/v1.0"


# Views are defined lazily as read_parquet over the hive layout. Globbing the
# whole prefix costs an S3 LIST of ~6k partitions, so single-day questions
# should use an explicit path instead (the agent prompt covers this).
def _view(chain: str, table: str) -> str:
    return f"read_parquet('{S3_BASE}/{chain}/{table}/*/*.parquet', hive_partitioning=1)"


VIEW_DEFS = {
    "btc_blocks": _view("btc", "blocks"),
    "btc_transactions": _view("btc", "transactions"),
    "eth_blocks": _view("eth", "blocks"),
    "eth_transactions": _view("eth", "transactions"),
    "eth_token_transfers": _view("eth", "token_transfers"),
    "eth_logs": _view("eth", "logs"),
}

# The recent-slice tables (all non-raw legs) share this shape: same 5.3M-row
# BTC slice (2026-08-20..28), loaded once from the raw Parquet.
SLICE_COLUMNS = (
    "txid, block_number, date (VARCHAR), fee, output_value, "
    "input_count, output_count, is_coinbase, size"
)

_RAW_DOC = f"""
## Available data (AWS Public Blockchain dataset, updated daily)

Base path: {S3_BASE}/  — hive-partitioned by date=YYYY-MM-DD

| Table (view) | Path | Notes |
|---|---|---|
| btc_blocks | {S3_BASE}/btc/blocks/ | Bitcoin blocks since 2009-01-03 |
| btc_transactions | {S3_BASE}/btc/transactions/ | Bitcoin txs; ~500MB parquet per recent day |
| eth_blocks | {S3_BASE}/eth/blocks/ | Ethereum blocks since 2015-07-30 |
| eth_transactions | {S3_BASE}/eth/transactions/ | Ethereum txs; value in wei |
| eth_token_transfers | {S3_BASE}/eth/token_transfers/ | ERC-20/721 transfers |
| eth_logs | {S3_BASE}/eth/logs/ | Raw event logs |
"""

# One entry per optional catalog leg: (alias, heading, how it resolves a table
# name to data). The "when to use" table below is assembled from active legs.
_LEG_DOCS = {
    "s3t": (
        "### `s3t` — S3 Tables native Iceberg REST catalog",
        "`s3t.blockchain.btc_transactions` — recent BTC slice. Resolution: one "
        "REST pointer lookup (S3 Tables service) + Iceberg manifest reads, zero "
        "S3 LIST. The catalog service owns the current-version pointer.",
    ),
    "glue": (
        "### `glue` — same Iceberg tables via AWS Glue Data Catalog (SageMaker Lakehouse)",
        "`glue.blockchain.btc_transactions` — THE SAME physical table as "
        "`s3t.blockchain.btc_transactions`, resolved through the Glue Iceberg "
        "REST endpoint (federated s3tablescatalog) instead of the S3 Tables "
        "native endpoint. Same data, one more governance hop.",
    ),
    "dl": (
        "### `dl` — DuckLake (SQL-database catalog, Parquet data on S3)",
        "`dl.blockchain.btc_transactions` — same BTC slice. All metadata "
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
   history list ~6000 partitions and read TBs — never do that.
2. For a SINGLE day on raw data, prefer an explicit path — avoids the S3 LIST:
   read_parquet('{S3_BASE}/btc/transactions/date=2026-08-25/*.parquet')
3. For a date RANGE, use the pre-defined view + WHERE date BETWEEN ... (partition
   pruning applies after listing).
4. Start with DESCRIBE or LIMIT 5 to learn the schema before aggregating.
5. ETH values are in wei (divide by 1e18 for ETH). BTC values in `outputs` are in
   satoshi inside a nested list column; output_value on transactions is BTC.
"""


def build_catalog_doc(legs: list[str]) -> str:
    """Assemble the system-prompt catalog doc for the active legs.

    `legs` is the list of attached optional catalogs ('s3t', 'glue', 'dl');
    the raw-Parquet views are always present.
    """
    parts = [_RAW_DOC]
    if legs:
        parts.append(
            "\n## Catalog-based access methods — same data, different resolution\n\n"
            f"All catalogs below hold the same recent BTC slice (2026-08-20..2026-08-28,"
            f" 5.3M rows) with columns: {SLICE_COLUMNS}.\n"
            "Prefer a catalog when its slice covers the question (no S3 LIST);"
            " prefer raw views for history before 2026-08-20.\n"
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
