# Design Notes — Data Agent on DuckDB

Design rationale and invariants behind the sample. The README covers what it
does and how to run it; this document covers why it is built this way.

## Architecture

```
User ──▶ AgentCore Runtime (1 session = 1 microVM = 1 DuckDB)
          ├─ AuthN: inbound JWT authorizer → claims {tenant, role}
          ├─ AuthZ: governance.py — principal→role→policy,
          │         sqlglot RLS/CLS rewrite BEFORE the engine
          ├─ Strands Agent + Bedrock Claude (writes SQL, reads results)
          └─ DuckDB in-process (httpfs/iceberg/ducklake extensions)
               ├─ Path A: raw Parquet   — S3 path glob (public dataset)
               ├─ Path B: S3 Tables     — native Iceberg REST catalog
               ├─ Path C: Glue catalog  — same table, federated endpoint
               └─ Path D: DuckLake      — SQL-database catalog, Parquet on S3
Aux: IAM execution role (least privilege) · CloudWatch Logs
```

The layering follows one principle: **externalize everything except compute**.
State lives on S3, governance in a SQL-rewrite layer in front of the engine,
scheduling in the agent platform (one session = one microVM), and the
analytics engine itself runs in-process with the agent.

## Design invariants (do not regress)

1. **Read-only gate** on `run_sql`: statement allowlist (read statements plus
   `CREATE TEMP TABLE|VIEW`), no persistent writes, no ATTACH of foreign
   databases, no COPY TO.
2. **In-memory database**, dies with the session. No local state to leak.
3. **Resource envelope** via env (`DUCKDB_MEMORY_LIMIT`); the engine obeys and
   spills to disk rather than negotiating — "slower, not dead".
4. **Cost discipline in the prompt**: always filter on the partition column;
   prefer explicit single-day paths over globs.
5. Errors are returned verbatim to the model — self-repair is a feature, not a
   bug path.
6. **Governance before the engine**: every statement passes the
   identity-aware rewrite (RLS/CLS injection, raw-path rejection) after the
   read-only gate and before execution; restricted principals fail closed on
   unparseable SQL. The engine never sees ungoverned SQL for a restricted
   principal.

## Why the governance layer is a SQL rewrite

Lake Formation row/column data filters are enforced *inside* LF-integrated
engines (Athena, Redshift, EMR). For a third-party in-process engine, LF
grants stop at table level — so fine-grained governance for an embedded
engine must happen before the SQL reaches it. `governance.py` implements
that as an engine-neutral sqlglot rewrite: every reference to a governed
logical table (via any of the four access paths) is replaced by
`(SELECT * EXCLUDE(denied) FROM ref WHERE row_filter) AS alias`.

Two details worth knowing:

- **Schema-aware CLS**: the same logical table can expose different columns
  per access method (a full-history raw glob binds the oldest file's schema,
  which may lack newer columns), so EXCLUDE lists are intersected with a
  DESCRIBE-probed, cached schema per reference; probe failure keeps the full
  denylist (fail closed).
- **Concurrency**: agent frameworks may execute same-message tool calls on
  threads. Unsynchronized `execute`+`fetch` pairs on one shared DuckDB
  connection steal each other's result sets, so `run_sql` is serialized with
  a lock. A per-thread cursor was rejected because temp tables must stay
  session-visible across calls.

## Measured behavior (reproducible with the included scripts)

- **In-process vs. query service**: [ab-results.md](ab-results.md) — the same
  5-query agent sequence (including one deliberate mistake) over the same
  Parquet files: DuckDB 3.9 s total vs Athena 8.1 s (2.1x), and the failed
  query costs 0.18 s in-process vs 0.51 s + queue via the service. Reproduce
  with `scripts/ab_compare.py`.
- **Access-path equivalence**: one session running the same day-slice
  aggregation over all four paths returns identical results (620,604 rows in
  the sample dataset), with per-path `engine_ms` reported.
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
