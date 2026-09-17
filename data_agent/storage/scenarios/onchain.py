"""On-chain fund-flow investigation on the AWS Public Blockchain dataset.

Public dataset: s3://aws-public-blockchain (region us-east-2, public read)
Layout: v1.0/eth/{blocks,transactions,logs,token_transfers,traces,contracts}/
        date=YYYY-MM-DD/*.parquet

The star table is `eth_traces` — internal calls, the only place a
contract-mediated drain (which never appears in the top-level `transactions`
table) is visible. All public tables are hive-partitioned by `date` (string,
YYYY-MM-DD); the cost gate requires a `date` predicate on every raw view.

Two small label tables give the raw ledger the attribution the analyst needs
(the public dataset has addresses and amounts but no entity labels):
  - `ofac_addresses`     — REAL public OFAC-sanctioned ETH addresses
  - `cex_deposit_labels` — SYNTHETIC exchange deposit-address labels
They are views over Parquet on the solution's own bucket, gated by the
LABELS_PREFIX env var (empty = off, so raw-only runs still work).

Personas (default policies, overridable via GOVERNANCE_POLICIES):
  exchange-a:investigator — case worker at exchange A: sees only the case's
    date window on the ledger, and only its OWN exchange's deposit labels
  exchange-a:junior      — same row scope, but the deposit labels'
    subject_identity (customer PII linkage) is column-denied
"""

from __future__ import annotations

import os
import re

from .base import Scenario, TableCard

S3_BASE = "s3://aws-public-blockchain/v1.0"


# Views are read_parquet over the hive layout. Globbing the whole prefix costs
# an S3 LIST of thousands of partitions, which is why the cost gate expands a
# literal date predicate into explicit per-day paths before the engine runs.
def _view(chain: str, table: str) -> str:
    return f"read_parquet('{S3_BASE}/{chain}/{table}/*/*.parquet', hive_partitioning=1)"


VIEW_DEFS = {
    "eth_blocks": _view("eth", "blocks"),
    "eth_transactions": _view("eth", "transactions"),
    "eth_token_transfers": _view("eth", "token_transfers"),
    "eth_traces": _view("eth", "traces"),
    "eth_logs": _view("eth", "logs"),
}


def _partitioned(chain: str, table: str) -> dict:
    """Cost-policy spec for a hive-partitioned raw source."""
    return {
        "column": "date",
        "granularity": "day",
        "base": f"{S3_BASE}/{chain}/{table}",
        "layout": "{base}/date={part}/*.parquet",
        "reader": "read_parquet",
        "args": "hive_partitioning=1",
    }


PARTITIONED_SOURCES = {
    "eth_blocks": _partitioned("eth", "blocks"),
    "eth_transactions": _partitioned("eth", "transactions"),
    "eth_token_transfers": _partitioned("eth", "token_transfers"),
    "eth_traces": _partitioned("eth", "traces"),
    "eth_logs": _partitioned("eth", "logs"),
}

# ------------------------------------------------------------------- labels
# Label views resolve to single Parquet files on the solution bucket (tiny —
# no hive layout, no partitioning). Built only when LABELS_PREFIX is set.
LABEL_TABLES = ("ofac_addresses", "cex_deposit_labels")

# LABELS_PREFIX is interpolated into read_parquet('<prefix>/<table>.parquet'),
# and the tables it points at decide the OUTCOME of a screening (a swapped
# ofac_addresses is a forged sanctions list). So the value is not trusted as
# given: strict s3:// syntax only (no quotes, spaces, parentheses or other
# schemes — nothing that could leave the literal), and the bucket must be one
# the operator owns. With no explicit allowlist that means the bucket the CDK
# stack provisions (duckdb-agent-ducklake-<account-id>).
_S3_PREFIX_RE = re.compile(
    r"^s3://(?P<bucket>[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?)(?P<key>(?:/[A-Za-z0-9._-]+)*)/?$"
)
SOLUTION_BUCKET_RE = re.compile(r"^duckdb-agent-ducklake-\d{12}$")


def validate_labels_prefix(prefix: str, allowed_buckets: frozenset[str] | None = None) -> str:
    """Return the normalized `s3://bucket/key` prefix or raise ValueError.

    `allowed_buckets` is the operator's explicit allowlist (LABELS_ALLOWED_BUCKETS);
    when None, only the solution-owned bucket name pattern qualifies. Called at
    import so a bad value fails the process instead of serving queries.
    """
    m = _S3_PREFIX_RE.match(prefix.strip())
    if not m:
        raise ValueError(
            "LABELS_PREFIX must be an s3://<bucket>/<key> prefix using only "
            "[A-Za-z0-9._-] path characters"
        )
    bucket = m.group("bucket")
    if any(seg in (".", "..") for seg in m.group("key").split("/")):
        raise ValueError("LABELS_PREFIX must not contain '.' or '..' path segments")
    if allowed_buckets is not None:
        if bucket not in allowed_buckets:
            raise ValueError(
                f"LABELS_PREFIX bucket {bucket!r} is not in LABELS_ALLOWED_BUCKETS "
                f"{sorted(allowed_buckets)}"
            )
    elif not SOLUTION_BUCKET_RE.match(bucket):
        raise ValueError(
            f"LABELS_PREFIX bucket {bucket!r} is not the solution-owned bucket "
            "(duckdb-agent-ducklake-<account-id>); set LABELS_ALLOWED_BUCKETS to allow it"
        )
    return f"s3://{bucket}{m.group('key')}"


def build_label_views(prefix: str, allowed_buckets: frozenset[str] | None = None) -> dict[str, str]:
    """View defs for the label tables under a validated s3:// prefix."""
    if not prefix:
        return {}
    base = validate_labels_prefix(prefix, allowed_buckets)
    return {name: f"read_parquet('{base}/{name}.parquet')" for name in LABEL_TABLES}


LABELS_PREFIX = os.environ.get("LABELS_PREFIX", "")
LABELS_ALLOWED_BUCKETS = (
    frozenset(
        b.strip() for b in os.environ.get("LABELS_ALLOWED_BUCKETS", "").split(",") if b.strip()
    )
    or None
)
if LABELS_PREFIX:
    validate_labels_prefix(LABELS_PREFIX, LABELS_ALLOWED_BUCKETS)  # fail-stop on misconfiguration

# ---------------------------------------------------------------------- doc
RAW_DOC = f"""
## Available data (AWS Public Blockchain dataset, Ethereum, updated daily)

Base path: {S3_BASE}/  — hive-partitioned by date=YYYY-MM-DD

| Table (view) | Path | Notes |
|---|---|---|
| eth_blocks | {S3_BASE}/eth/blocks/ | Ethereum blocks since 2015-07-30 |
| eth_transactions | {S3_BASE}/eth/transactions/ | Top-level txs; `value` wei; from_address, to_address |
| eth_token_transfers | {S3_BASE}/eth/token_transfers/ | ERC-20/721: token_address, from_address, to_address, `value` |
| eth_traces | {S3_BASE}/eth/traces/ | INTERNAL calls: from_address, to_address, `value`, call_type, trace_type, `status` — a contract-mediated transfer shows up ONLY here, never in eth_transactions |
| eth_logs | {S3_BASE}/eth/logs/ | Raw event logs: `address`, `topics` (list), `data` |
"""  # noqa: E501 — markdown table rows in the system-prompt doc

LABEL_DOC = """
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


# ---------------------------------------------------------------- cards
# Table cards: grain, when to use, when not to, caveats, and the
# reconciled column list. Column names below are the dataset's real schema
# (DESCRIBE of the 2025-02-21 partition, 2026-09-09); `date` is the hive
# partition column. tests/unit/test_reconcile.py checks the prompt doc and the
# policies against these; scripts/reconcile.py checks these against the engine.
_ETH_OWNER = "onchain scenario (AWS Public Blockchain, eth v1.0)"
CARDS = {
    "eth_blocks": TableCard(
        grain="one row = one Ethereum block",
        use_when="block metadata: timestamps, miner, gas limits, tx counts per block",
        avoid_when="tracing value transfers — blocks carry no transfer rows",
        caveats="`timestamp` is the block time; join transactions on block_number",
        owner=_ETH_OWNER,
        columns=(
            "difficulty",
            "hash",
            "miner",
            "nonce",
            "number",
            "size",
            "timestamp",
            "total_difficulty",
            "base_fee_per_gas",
            "gas_limit",
            "gas_used",
            "extra_data",
            "logs_bloom",
            "parent_hash",
            "state_root",
            "receipts_root",
            "transactions_root",
            "sha3_uncles",
            "transaction_count",
            "date",
            "last_modified",
        ),
    ),
    "eth_transactions": TableCard(
        grain="one row = one top-level (externally signed) transaction",
        use_when="who signed what: sender, gas paid, receipt status, contract creation",
        avoid_when=(
            "following a contract-mediated drain — internal value moves are only in eth_traces"
        ),
        caveats=(
            "`value` is wei (divide by 1e18); `hash` is "
            "the tx hash (eth_traces calls it transaction_hash)"
        ),
        owner=_ETH_OWNER,
        columns=(
            "gas",
            "hash",
            "input",
            "nonce",
            "value",
            "block_number",
            "block_hash",
            "transaction_index",
            "from_address",
            "to_address",
            "gas_price",
            "receipt_cumulative_gas_used",
            "receipt_gas_used",
            "receipt_contract_address",
            "receipt_status",
            "receipt_effective_gas_price",
            "transaction_type",
            "max_fee_per_gas",
            "max_priority_fee_per_gas",
            "block_timestamp",
            "date",
            "last_modified",
        ),
    ),
    "eth_token_transfers": TableCard(
        grain="one row = one ERC-20/721 Transfer event",
        use_when="token (not ETH) movements: which token, from, to, amount",
        avoid_when="native ETH flows — those are eth_traces / eth_transactions",
        caveats=(
            "`value` is in the token's own decimals, not wei; token_address identifies the contract"
        ),
        owner=_ETH_OWNER,
        columns=(
            "token_address",
            "from_address",
            "to_address",
            "value",
            "transaction_hash",
            "log_index",
            "block_number",
            "block_hash",
            "block_timestamp",
            "date",
            "last_modified",
        ),
    ),
    "eth_traces": TableCard(
        grain="one row = one internal call (EVM trace) inside a transaction",
        use_when=(
            "following the money: every ETH value move incl. contract-mediated ones, fan-out, hops"
        ),
        avoid_when="counting user-signed transactions — use eth_transactions",
        caveats=(
            "filter value > 0 AND status = 1 for successful "
            "transfers; a day is a few GB, always add an address filter"
        ),
        owner=_ETH_OWNER,
        columns=(
            "block_number",
            "transaction_hash",
            "transaction_index",
            "error",
            "status",
            "trace_id",
            "subtraces",
            "trace_address",
            "block_hash",
            "trace_type",
            "gas",
            "gas_used",
            "from_address",
            "value",
            "reward_type",
            "input",
            "output",
            "call_type",
            "to_address",
            "block_timestamp",
            "date",
            "last_modified",
        ),
    ),
    "eth_logs": TableCard(
        grain="one row = one emitted event log",
        use_when="decoding contract events beyond Transfer: topics[] and data of any log",
        avoid_when="token transfers — eth_token_transfers is already decoded",
        caveats="`topics` is a list (topics[1] is the event signature); `data` is hex",
        owner=_ETH_OWNER,
        columns=(
            "address",
            "topics",
            "data",
            "transaction_index",
            "log_index",
            "transaction_hash",
            "block_number",
            "block_hash",
            "block_timestamp",
            "date",
            "last_modified",
        ),
    ),
    "ofac_addresses": TableCard(
        grain="one row = one OFAC SDN-listed crypto address",
        use_when="sanctions screening: join on lower(address)",
        avoid_when="attributing exchanges or customers — that is cex_deposit_labels",
        caveats="real public list, ~120 rows; `program` is the sanctions program",
        owner="onchain scenario (public OFAC SDN crypto list)",
        columns=("address", "program", "source"),
    ),
    "cex_deposit_labels": TableCard(
        grain="one row = one exchange deposit address with its label",
        use_when="cash-out points: which recipients are exchange deposit addresses",
        avoid_when="anything needing real attribution — this table is SYNTHETIC",
        caveats=(
            "subject_identity is customer PII linkage "
            "(column-denied for junior roles); rows are tenant-scoped by exchange"
        ),
        owner="onchain scenario (synthetic sample)",
        columns=("address", "exchange", "label", "subject_identity", "risk_tier"),
    ),
}

# ----------------------------------------------------------------- policies
# Table-level deny-by-default: a restricted persona may reference
# ONLY the tables its policy names (plus session temp objects it creates).
# So each persona must list every table it legitimately reads — an empty rule
# `{}` means "readable, no row/column restriction". The public ledger context
# tables and the public OFAC list are readable as-is; the row/column scoping
# that the demo is about lives on `eth_traces` (the incident window) and
# `cex_deposit_labels` (own-exchange rows; junior also loses subject_identity).
# A table NOT listed here (e.g. the cloud-ops tables mounted by the other
# scenario) is refused for these personas — the cross-scenario isolation the
# T001 finding was about.
_LEDGER_CONTEXT = {  # public Ethereum context tables — readable, cost-gated by date
    "eth_transactions": {},
    "eth_blocks": {},
    "eth_token_transfers": {},
    "eth_logs": {},
    "ofac_addresses": {},  # public OFAC sanctions list
}
POLICIES = {
    "exchange-a:investigator": {
        "allow_raw_paths": False,
        "tables": {
            **_LEDGER_CONTEXT,
            # ledger: only the open-case incident window
            "eth_traces": {"row_filter": "date >= '2025-02-21'"},
            # attribution: this tenant sees only its own exchange's labels
            "cex_deposit_labels": {"row_filter": "exchange = 'exchange-a'"},
        },
    },
    "exchange-a:junior": {
        "allow_raw_paths": False,
        "tables": {
            **_LEDGER_CONTEXT,
            "eth_traces": {"row_filter": "date >= '2025-02-21'"},
            "cex_deposit_labels": {
                "row_filter": "exchange = 'exchange-a'",
                "deny_columns": ["subject_identity"],
            },
        },
    },
}


def build(
    labels_prefix: str = LABELS_PREFIX,
    allowed_buckets: frozenset[str] | None = LABELS_ALLOWED_BUCKETS,
) -> Scenario:
    """The scenario as configured by env: raw views always, labels when set."""
    labels = build_label_views(labels_prefix, allowed_buckets)
    return Scenario(
        name="onchain",
        views={**VIEW_DEFS, **labels},
        sources=dict(PARTITIONED_SOURCES),
        doc=RAW_DOC + (LABEL_DOC if labels else ""),
        policies=POLICIES,
        cards=CARDS,
    )


SCENARIO = build()
