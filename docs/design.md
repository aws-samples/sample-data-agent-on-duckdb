# Design Notes — Data Agent on DuckDB

Design rationale and invariants behind the sample. The README covers what it
does and how to run it; this document covers why it is built this way.

## Architecture

```
User ──▶ Streamlit chat UI ── Cognito login → ID token (JWT)
              │  raw HTTPS POST /invocations + Bearer token + session id
              ▼
        AgentCore Runtime (1 session = 1 microVM = 1 DuckDB)
          ├─ AuthN: managed inbound JWT authorizer — signature + audience
          │         validated BEFORE agent code runs
          ├─ AuthZ: governance.py — verified claims → role policy,
          │         sqlglot RLS/CLS rewrite BEFORE the engine
          ├─ Strands Agent + Bedrock Claude (writes SQL, reads results)
          │    └─ streaming: typed SSE envelope, per-SQL gate→govern→execute
          └─ DuckDB in-process (httpfs/iceberg/ducklake extensions)
               ├─ Path A: raw Parquet   — S3 path glob (public dataset)
               ├─ Path B: S3 Tables     — native Iceberg REST catalog
               ├─ Path C: Glue catalog  — same table, federated endpoint
               ├─ Path D: DuckLake      — SQL-database catalog, Parquet on S3
               └─ label tables — OFAC list (real, public) + synthetic
                  exchange deposit labels (attribution joins)
Aux: IAM execution role (least privilege) · CloudWatch Logs
```

The layering follows one principle: **externalize everything except compute**.
State lives on S3, authentication in the platform's managed authorizer,
authorization in a SQL-rewrite layer in front of the engine, scheduling in
the agent platform (one session = one microVM), and the analytics engine
itself runs in-process with the agent.

## Design invariants (do not regress)

1. **Read-only gate** on `run_sql`: statement allowlist, **exactly one
   statement per call** (checked with the engine's own parser,
   `duckdb.extract_statements`; leading SQL comments are stripped before
   keyword classification), no persistent writes, no ATTACH of foreign
   databases, no COPY TO. The single-statement rule is load-bearing, not
   tidiness: the allowlist matches the leading keyword only.
2. **In-memory database**, dies with the session. No local state to leak.
3. **Resource envelope** via env (`DUCKDB_MEMORY_LIMIT`); the engine obeys and
   spills to disk rather than negotiating — "slower, not dead".
4. **Cost discipline in the prompt**: always filter on the partition column;
   prefer explicit single-day paths over globs.
5. Errors are returned verbatim to the model — self-repair is a feature, not a
   bug path.
6. **Governance before the engine**: every statement passes the
   identity-aware rewrite (RLS injection, schema-aware CLS, raw-path
   allowlisting) after the read-only gate and before execution; restricted
   principals fail closed on unparseable SQL. The engine never sees
   ungoverned SQL for a restricted principal.

## Authentication (real, not asserted)

The runtime is deployed with AgentCore's **managed inbound JWT authorizer**
pointed at a Cognito user pool (CDK-provisioned): token signature and
audience are validated by the platform before agent code runs; requests
without a valid token are rejected with 401/403 pre-code. Agent code then
reads the *verified* claims (`custom:tenant` / `custom:role`) to select the
governance policy.

Implementation notes that save the next person a day:

- Cognito puts **custom attributes in the ID token**, not the access token —
  the client must send the ID token as the Bearer.
- The authorizer must therefore validate with **allowedAudience** (matches
  the ID token's `aud` = app client id). `allowedClients` validates
  `client_id`, which only exists in access tokens — ID tokens would be
  rejected with a client-id mismatch.
- With an OAuth authorizer configured, the boto3 `invoke_agent_runtime` SDK
  path is not usable: the client makes a **raw HTTPS POST** to the runtime's
  `/invocations` endpoint with the Bearer header and a session-id header
  (`ui/app.py` and `scripts/jwt_invoke.py` both carry this pattern).
- The SigV4 fallback identity paths (runtime user-id header, payload
  identity) remain for local/CLI use; they trust the IAM-authenticated
  caller to assert its user's identity — a documented trust assumption.

## Why the governance layer is a SQL rewrite

Lake Formation row/column data filters are enforced *inside* LF-integrated
engines (Athena, Redshift, EMR). For a third-party in-process engine, LF
grants stop at table level — so fine-grained governance for an embedded
engine must happen before the SQL reaches it. `governance.py` implements
that as an engine-neutral sqlglot rewrite: every reference to a governed
logical table (via any of the four access paths) is replaced by
`(SELECT * EXCLUDE(denied) FROM ref WHERE row_filter) AS alias`.

Details that matter for correctness (each is regression-tested):

- **Fail-closed everywhere**: unparseable SQL is rejected for restricted
  principals; a failed schema probe keeps the full column denylist; an
  unknown role within a known tenant resolves to that tenant's **most
  restricted** policy (content-derived ranking, independent of declaration
  order).
- **Raw table functions are allowlisted by AST shape**, not denylisted by
  name: only plain identifier FROM/JOIN sources are allowed for restricted
  principals (pure generators like `range()` excepted) — a denylist of
  `read_parquet/read_csv/...` misses aliases like `parquet_scan`.
- **Explicit selection of a denied column is a clean policy denial** naming
  the column and role — not a confusing engine binder error the model then
  works around. The boundary is visible in the UI trace.
- **Schema-aware CLS**: the same logical table exposes different columns per
  access method, so EXCLUDE lists are intersected with a DESCRIBE-probed,
  cached schema per reference.
- **Concurrency**: agent frameworks may execute same-message tool calls on
  threads. `run_sql` is serialized with a lock (temp tables must stay
  session-visible, so per-thread cursors were rejected); the governance
  schema probe takes the same lock.

## Streaming trace (why the UI can show the engine live)

The entrypoint is an async generator, so AgentCore serves native SSE. Strands
events are wrapped in a small typed envelope
(`start | reasoning | text | tool_start | trace | result`); `run_sql` itself
is an async-generator tool that yields one trace event per phase —
`gate` (raw SQL), `govern` (principal, injected RLS predicates, hidden
columns, or a clean rejection), `execute` (engine, `engine_ms`, rows) — and
the final result event carries the consolidated trace array. The Streamlit
UI renders the transcript in true arrival order and totals engine time on a
Trace tab. One practical note: tool-input deltas stream per character, so
tool cards must coalesce events by `tool_use_id`.

## The demo scenario (and why it fits the pattern)

On-chain fund-flow investigation over public Ethereum data
(`s3://aws-public-blockchain`): an investigation is a branching, multi-hop
query tree — each query depends on the previous result, over a case-scoped
working set interrogated dozens of times. That is the in-process engine's
sweet spot (build the working set once as a temp table, then sub-ms
follow-ups) and a poor fit for a re-scan-per-query remote service.

The attribution tables are two small label sets: `ofac_addresses` (the real,
public OFAC SDN address list) and `cex_deposit_labels` (**synthetic** sample
records whose `subject_identity` column mimics the KYC linkage a real
exchange would hold — that column is exactly what the CLS policy protects in
the demo). The agent, governance, and UI code are scenario-agnostic; the
dataset, catalog doc, and policy table are the swap points.

## Measured behavior (reproducible with the included scripts)

- **In-process vs. query service**: [ab-results.md](ab-results.md) — the same
  5-query agent sequence (including one deliberate mistake) over the same
  Parquet files: DuckDB 3.9 s total vs Athena 8.1 s (2.1x), and the failed
  query costs 0.18 s in-process vs 0.51 s + queue via the service. Reproduce
  with `scripts/ab_compare.py`.
- **Session working-set reuse**: in-process temp-table follow-ups measure
  median 0.5 ms / p90 1.15 ms. For calibration against remote engines:
  Athena has no session temp tables (CTAS persists and must be cleaned up);
  Redshift Serverless *can* hold a session temp table across Data API calls
  (SessionKeepAliveSeconds), but each reuse is still a metered, serialized
  network round-trip (~360–400 ms engine-side measured) with session caps —
  the in-process advantage is local memory reads with zero session
  lifecycle, a 20–600x gap, not a "remote cannot hold state" absolute.
- **Access-path equivalence**: one session running the same aggregation over
  all four paths returns identical results, with per-path `engine_ms`.
- **Resource envelope**: with `DUCKDB_MEMORY_LIMIT=256MB` plus a spill
  directory, a full-day sort completes in seconds rather than failing.
- **Concurrency isolation**: `scripts/concurrent_invoke.sh N` runs N parallel
  sessions with independent latencies — no queue, no cross-talk.

## DuckLake catalog forms

The default Path D catalog is a single file on S3 (READ_ONLY mount,
concurrent readers safe, zero extra infrastructure). For multi-writer
estates — ingestion pipelines committing snapshots while agents read —
switch `DUCKLAKE_CATALOG` to the `postgres:` form (an RDS PostgreSQL
catalog; credentials via `DUCKLAKE_PG_SECRET` in Secrets Manager; requires
a VPC-attached runtime to reach RDS, and RDS Proxy is recommended for the
many short-lived microVM connections). Both forms are implemented.

## When to use what

| Situation | Recommendation |
|---|---|
| Data in a governed warehouse, few heavy queries | Warehouse MCP direct, skip DuckDB |
| Data on S3, agent iterates heavily, unpredictable concurrency | This pattern |
| Row/column-level access control per tenant | This pattern — built-in rewrite layer (LF data filters only work inside trusted engines) |
| Both (most real estates) | Hybrid: DuckDB fast path + warehouse MCP fallback |
| Working set approaches single-node memory | Split the query or move up the spectrum (Athena/Redshift) |
