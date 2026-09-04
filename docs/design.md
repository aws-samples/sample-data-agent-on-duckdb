# Design Notes — Data Agent on DuckDB

Design rationale and invariants behind the sample. The README covers what it
does and how to run it; this document covers why it is built this way.

## Architecture — five layers

A traditional database owns everything: storage, catalog, permissions,
scheduling and execution. This solution keeps only execution in-process and
externalizes the rest, then adds the one layer an *agent* needs that a
database does not: a context layer between the model and the engine. Each
layer has one responsibility and one place to live, listed in the order a
request flows through them. The layering is additive — a semantic layer is
the likely next one. Governance and Context are deliberately separate:
Governance is authorization and nothing else; Context is what the model sees
and what the engine is asked to do. Storage is the durable data on S3 and
nothing else — session working sets and `_r_<n>` result tables are engine
temp tables and belong to Compute.

| Layer | Lives in | Responsibility | Mechanism |
|---|---|---|---|
| Scheduling | AgentCore Runtime; `data_agent/scheduling/` is what it calls into | session and resource management | one session = one microVM = one engine; claims in, streamed envelopes out |
| Context | `data_agent/context/` (datamap · cost · results · compression · prompt) | what the model sees, what the engine is asked | data map from the engine catalog; partition-filter cost gate + path expansion; shape not truncate; measured-pressure history stubs |
| Governance | `data_agent/governance/` (policies · hardening · rewrite, sqlglot) | authorization | RLS/CLS rewrite before the engine, from verified JWT claims; fail closed; total by construction; universal hardening |
| Compute | in-process DuckDB (`data_agent/compute/`) | SQL execution | engine inside the agent process, S3 read in place; working sets and `_r_<n>` results kept as session temp tables |
| Storage | Amazon S3 (`data_agent/storage/`: legs + scenarios) | durable data | Parquet / native exports, Iceberg (S3 Tables, Glue), DuckLake — four access paths to the same data |

```
User ──▶ Streamlit chat UI ── Cognito login → ID token (JWT)
              │  raw HTTPS POST /invocations + Bearer token + session id
              ▼
        AgentCore Runtime (1 session = 1 microVM = 1 DuckDB)
          ├─ AuthN: managed inbound JWT authorizer — signature + audience
          │         validated BEFORE agent code runs
          ├─ Strands Agent + Bedrock Claude (writes SQL, reads shaped results)
          │    └─ streaming: typed SSE envelope, per-SQL gate→govern→cost→execute
          ├─ Context layer      — data map · cost gate · result shaping · history stubs
          ├─ Governance layer   — verified claims → role policy,
          │                       sqlglot RLS/CLS rewrite + hardening BEFORE the engine
          └─ DuckDB in-process (httpfs/iceberg/ducklake/fts extensions)
               ├─ Path A: raw Parquet / native exports — S3 path glob → explicit partitions
               ├─ Path B: S3 Tables     — native Iceberg REST catalog
               ├─ Path C: Glue catalog  — same table, federated endpoint
               ├─ Path D: DuckLake      — SQL-database catalog, Parquet on S3
               ├─ label tables — OFAC list (real, public) + synthetic exchange labels
               └─ _r_<n> result tables · session working sets (temp, die with the session)
Aux: IAM execution role (least privilege) · CloudWatch Logs (EMF audit metrics)
```

The layering follows one principle: **externalize everything except compute,
and put the agent's own concerns in a layer of their own.** State lives on
S3, authentication in the platform's managed authorizer, authorization in a
SQL-rewrite layer in front of the engine, scheduling in the agent platform
(one session = one microVM), the analytics engine runs in-process with the
agent, and the context layer decides what the model sees and what the engine
is asked.

## Design invariants (do not regress)

1. **Read-only gate** on `run_sql`: statement allowlist, **exactly one
   statement per call** (checked with the engine's own parser,
   `duckdb.extract_statements`; leading SQL comments are stripped before
   keyword classification), no persistent writes, no ATTACH of foreign
   databases, no COPY TO. The single-statement rule is load-bearing, not
   tidiness: the allowlist matches the leading keyword only.
2. **In-memory database**, dies with the session. No local state to leak.
   Framework-materialized result tables (`_r_<n>`) are session temp tables
   under the same rule — they are never written anywhere.
3. **Resource envelope** via env (`DUCKDB_MEMORY_LIMIT`); the engine obeys and
   spills to disk rather than negotiating — "slower, not dead". Result
   materialization is bounded too (`MAX_MATERIALIZE_ROWS`, `RESULT_TABLE_LRU`).
4. **Cost discipline is enforced by the context layer's cost gate, guided by
   the prompt.** A scan of a partitioned raw source without a predicate on its
   partition column is rejected before the engine runs; literal partition
   predicates are expanded into explicit per-partition paths so the engine
   never LISTs the whole prefix. The prompt still teaches the discipline; the
   gate makes it non-negotiable. The gate runs in the same pre-engine pass as
   governance but is not an authorization concern.
5. Errors are returned verbatim to the model — self-repair is a feature, not a
   bug path.
6. **Governance before the engine**: every statement passes the
   identity-aware rewrite (RLS injection, schema-aware CLS, raw-path
   allowlisting) after the read-only gate and before execution; restricted
   principals fail closed on unparseable SQL. The engine never sees
   ungoverned SQL for a restricted principal.
7. **Governance is total, not just correct.** A statement shape the parser
   cannot model is rejected, a local name may not shadow a governed table,
   and a principal with no matching policy gets the built-in `deny-all`
   floor. Each rule closes a bypass that was executable against an earlier
   version of this code (see the coverage rules below).
8. **Every framework mechanism must depend on the engine being in-process to
   make sense** (the admission test for the context layer). Oversized results
   become temp tables *because* the engine is here; compressed history points
   into the engine *because* the data lives there; the table map is generated
   from the engine's catalog; cost policy is a rewrite in front of the engine.
   A mechanism that would work identically with a remote query service (a
   cheap side-model for summaries, a REST adapter layer, generic prompt
   compression) does not belong in this solution.
9. **Shape, never truncate.** No tool result is silently cut. A result the
   model should not read whole is stored in full and shown in a different
   shape — a schema summary with a retrieval path — never as "the first N
   rows", which reads as complete and misleads the model.

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
  (`ui/app.py`, `scripts/jwt_invoke.py` and `scripts/run_eval.py` all carry
  this pattern).
- The SigV4 fallback identity paths (runtime user-id header, payload
  identity) remain for local/CLI use; they trust the IAM-authenticated
  caller to assert its user's identity — a documented trust assumption.
  Local one-shot runs must therefore name a principal (`--as tenant:role`);
  a missing identity means deny-all, never full access.
- **The identity source must not be self-service.** The governance claims
  ride on Cognito custom attributes, and a CDK app client with default
  attribute permissions lets any signed-in user call
  `UpdateUserAttributes` on themselves — set `custom:role=admin`, sign in
  again, become an unrestricted principal. The stack therefore creates the
  client with `custom:tenant` / `custom:role` **readable but not writable**;
  role assignment is an operator action (`AdminUpdateUserAttributes`).

## Why the governance layer is a SQL rewrite

Lake Formation row/column data filters are enforced *inside* LF-integrated
engines (Athena, Redshift, EMR). For a third-party in-process engine, LF
grants stop at table level — so fine-grained governance for an embedded
engine must happen before the SQL reaches it. `data_agent/governance/`
implements that as an engine-neutral sqlglot rewrite: every reference to a
governed logical table (via any of the four access paths) is replaced by
`(SELECT * EXCLUDE(denied) FROM ref WHERE row_filter) AS alias`. The package
is authorization only: `policies.py` (the policy table, its validation,
`resolve_role`, the active role), `hardening.py` (rules for every principal),
`rewrite.py` (the RLS/CLS rewrite itself).

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
  cached schema per reference. The prober is wired into the pipeline from
  the compute layer so that compute never imports governance.
- **Concurrency**: agent frameworks may execute same-message tool calls on
  threads. `run_sql` is serialized with a lock (temp tables must stay
  session-visible, so per-thread cursors were rejected); the governance
  schema probe takes the same lock.

**Coverage rules.** Query-rewrite governance fails through gaps in what it
rewrites, not through wrong rewrites, so the rewrite is kept *total* by
construction. Each rule below closes a bypass that was proven executable
against an earlier version of this code, and each has adversarial
regression tests — the shadow shapes and the opaque statement forms are the
regression surface for any new governed table.

- **Statement roots are an allowlist** (`SELECT`, set operations,
  subqueries, `CREATE TEMP`, `DESCRIBE`, `SUMMARIZE`, `SHOW`). sqlglot
  renders anything it cannot model (`EXPLAIN`, `EXPLAIN ANALYZE`, `SHOW
  <table>`) as an opaque command with no table nodes, so a rewrite loop
  finds nothing to guard and passes it through — and `EXPLAIN ANALYZE`
  really executes the inner query, reporting denied-row cardinalities in
  the plan. A denylist's failure mode here is silent; the allowlist's is a
  visible rejection. `DESCRIBE`/`SUMMARIZE` of a governed table emit the
  guard as a subselect, and a `PIVOT` modifier moves onto the guard so it
  aggregates governed rows.
- **No local name may shadow a governed table.** A CTE, temp table/view,
  derived-table alias or table alias that reuses a governed table's name is
  refused. The alternative — exempting "local" names from the rewrite —
  let `WITH t AS (SELECT * FROM t) SELECT denied_col FROM t` unguard the
  real base table for both RLS and CLS. Scoping that exemption correctly
  across recursive CTEs, nested scopes and the engine's temp-schema search
  order is fragile; refusing the collision makes the invariant total, and
  analysts lose nothing (a working set is named `ws`, not after its source).
  The framework's `_r_` prefix is reserved under the same rule.
- **`deny-all` is the floor.** A principal whose claims match no policy gets
  the built-in deny-all policy and the entrypoint refuses before the model
  is invoked. Pointing the default at a permissive role turns "no
  identity" into "full access".
- **Configuration is validated at load.** `GOVERNANCE_POLICIES` keys must be
  `tenant:role`, tables and columns bare identifiers, each `row_filter`
  must parse as one predicate, and `allow_raw_paths` cannot be combined
  with governed tables — a typo fails the process rather than quietly
  widening access. `LABELS_PREFIX` is validated the same way and
  restricted to the solution-owned bucket unless `LABELS_ALLOWED_BUCKETS`
  says otherwise, because it decides what the sanctions screen compares
  against.
- **Universal hardening applies to every principal, the unrestricted role
  included.** The read-only gate classifies on the leading keyword, so
  `WITH x AS (SELECT 1) INSERT INTO t …` / `… DELETE …` read as "with" and
  the DML root executed; table functions that reach the container
  filesystem or the secret store (`read_text`, `read_blob`, `glob`,
  `getenv`, `duckdb_secrets`, `which_secret`) are plain "reads" the raw-path
  allowance never meant to include; and DuckDB's replacement scan turns a
  string literal such as `FROM '/etc/passwd'` into a file read while sqlglot
  parses it as an ordinary identifier. Hardening therefore rejects DML and
  non-temp CREATE roots even behind a CTE prefix, denies the host-reaching
  functions regardless of policy, and requires every file path to carry an
  allowed scheme (`RAW_PATH_SCHEMES`, default `s3://`, case-insensitive).
  Each of these was verified executable before the fix.

## The context layer: what the model sees, what the engine is asked

A good engine wired into a careless agent loop truncates results, bloats
context and re-scans S3. The context layer is where the in-process engine's
advantages are either realized or thrown away, so it is a layer of its own.
Its four mechanisms answer failure modes measured in production data agents:
results truncated to "3 rows (+276 more)" that the model read as complete; a
long investigation losing its key result to position-based history
compression; a catalog of hundreds of databases that cannot live in a
prompt; `EXPLAIN` that cannot be trusted as a cost gate.

Design constraint: **no multi-source adapters.** Every source lands on S3
(Parquet/Iceberg, or the service's native export format) and DuckDB reads it
there. This is what makes the hardest of those problems disappear: no Arrow
round-trip for oversized results, no external metadata cache for the data
map, no per-dialect cost heuristics. Invariant 8 is the admission test for
anything added here.

### Result shaping: materialize, summarize, read back

Replaces row-cap truncation (which violated invariant 9) with a three-way
dispatch that is one statement because the engine is in-process.

- Query-shaped statements (SELECT / WITH / set operations) execute as
  `CREATE TEMP TABLE _r_<n> AS <governed_sql> LIMIT MAX_MATERIALIZE_ROWS+1`;
  the LIMIT is appended textually when absent (no re-serialization of the
  model's SQL), a smaller literal LIMIT is kept, and otherwise the statement
  is wrapped in `SELECT * FROM (…) LIMIT n` — DuckDB keeps the inner ORDER BY
  under an outer LIMIT. DESCRIBE / SHOW / SUMMARIZE / user CTAS keep the
  direct path.
- `n = count(*)`. If `n ≤ MAX_RESULT_ROWS` and the JSON is `≤
  MAX_RESULT_CHARS`, the rows come back inline and the table is dropped
  (nothing lingers). Otherwise the table is kept and the model receives a
  **schema summary**: `{"shape":"summary","ref":"_r_7","rows":5213,
  "columns":[{name,type}…],"sample":[…3 rows…],"read_with":"SELECT … FROM
  _r_7 …"}` plus a fixed caveat naming the retrieval path and the
  `LIMIT 200 OFFSET n` paging pattern (asked for "the rows, not an
  aggregate", a model otherwise pages in chunks that re-materialize). Never
  "first N rows". A result that hit `MAX_MATERIALIZE_ROWS` is always a
  summary.
- Read-back is just `run_sql` against `_r_<n>` — governed rows already, zero
  copies, sub-millisecond. `_r_` is a reserved prefix (shadowing rule), and
  an LRU evicts the least-recently-queried table when `RESULT_TABLE_LRU` is
  exceeded, with reference tracking from the tables the governed AST names.
- Trace: the `execute` phase gains `shape: rows|summary`, `materialized`,
  `total_rows`, `evicted`.

Measured in the cloud: a 1,322-row Iceberg query materialized `_r_1` in about
2.5 s; the model then read it back seven times (paging, `COUNT(DISTINCT)`,
top-3 GROUP BY) at 3.7–6.2 ms each with no S3 re-scan.

### History compression: measured pressure, engine-pointer stubs

`ResultStubConversationManager` extends the Strands sliding-window manager
and uses its proactive-compression hook: `BeforeModelCallEvent.
projected_input_tokens`, computed from the last Bedrock `usage` report —
measure, don't guess.

- Trigger: projected tokens ≥ `COMPRESSION_THRESHOLD × context window`
  (default 60 %), and history ≥ `MIN_MESSAGES_FOR_STUB`. The first turn never
  compresses.
- Candidates: `run_sql` tool results outside the last two messages that
  carry a materialized `ref`. Order: **superseded first** (the same SQL
  executed again later), then oldest. Reclaim `(projected − budget) × 4`
  bytes per token and stop — linear to target, not "oldest 30 %".
- The stub doubles as the recap: `[Compressed] run_sql: <sql[:120]> → N rows,
  kept in temp table _r_7 — query it with run_sql.` The temp table is
  untouched; nothing is lost. Inline (small) results are stubbed only under
  a real overflow, and their stub keeps the SQL so they can be re-run.
- Reactive overflow (the provider rejected the call): stub everything
  eligible, then fall back to the sliding window. The message-count floor
  `MAX_CONTEXT_MESSAGES` (150) is secondary to the token lever.

### Data map: L0 generated, L1 zero-network search, L2 on demand

A hand-written table list in the prompt is fine for seven tables and a lie
at seven hundred.

- **L0** — the catalog section of the system prompt is generated from the
  engine's catalog (`duckdb_tables()` / `duckdb_views()`, attached legs
  included) at first invoke: `catalog.schema.table` names plus the partition
  column the cost gate requires. Hand-written text keeps only domain hints.
  This is what makes "point it at your own S3 tables" true at hundreds of
  tables. Attached Iceberg catalogs appear in `duckdb_tables()` but
  `duckdb_columns()` returns only a placeholder for them, so column names come
  from one bounded DESCRIBE per table (a single-partition explicit path for
  raw sources, `DATAMAP_PROBE_PARTITION`).
- **L1** — `search_schema(query)`: a session `_catalog` table (names,
  columns) indexed with DuckDB's `fts` extension, ranked with `match_bm25`.
  Zero network, zero LLM, engine-native; falls back to token overlap if
  `fts` is unavailable. Returns names only — governance applies when the
  model queries.
- **L2** — `DESCRIBE` via `run_sql` (the governance schema prober already
  caches these).
- Freshness is session-scoped by design: a dropped table errors on use and
  the next search rebuilds the index. No external cache — production data
  agents that added one measured thousands of lookups per search and
  removed it.

### Cost policy as a rewrite

Applied after governance and before execution — the second policy species
in the same pre-engine pass ("what may this cost" next to "who may see
what"), engine-neutral like the first.

- **Required-filter gate**: a reference to a partitioned raw source (the
  scenario views, or `read_parquet/read_json/read_csv` over one of their
  whole-prefix globs for the unrestricted role) without a predicate on its
  partition column in scope is rejected with an actionable message. Temp
  tables, `_r_<n>` tables and catalog-leg tables are exempt — the catalog
  prunes by metadata. Rationale: `EXPLAIN` estimates are not a trustworthy
  refusal basis on cloud engines; the predicate is the cost control.
- **Path expansion**: `WHERE date BETWEEN a AND b` / `= a` / `IN (…)` /
  `>= a AND <= b` on a raw view is rewritten to
  `read_parquet([<explicit per-partition paths>], hive_partitioning=true)`,
  bounded by `MAX_EXPANDED_PARTITIONS`, so the engine LISTs N partition
  prefixes instead of the whole table prefix. Unbounded or non-literal
  predicates satisfy the filter requirement and are left to the engine's own
  pruning. `scripts/ab_compare.py` measures LIST calls and wall time before
  and after.
- The rewrite composes with governance: an investigator's RLS lower bound
  and the caller's own `date <=` upper bound become explicit paths inside the
  guard subquery.
- Two model habits the rewrite handles rather than fights: an uppercase
  scheme (`'S3://…'`, the check is case-insensitive) and a bare prefix
  replacement scan (`FROM 's3://…/eth/traces/'`, recognised as the source).
  The prompt's query rules say "reference raw data by view name".
- Trace: a separate `cost` phase (emitted only when a partitioned source is
  touched) carries `sources`, `partition_filter`, `expanded_paths` and the
  rewritten SQL; the UI renders it as its own line.

Measured in the cloud: `SELECT count(*) FROM eth_traces` rejected before the
engine, the model added `date = '2025-02-21'`, one explicit path, ~0.6 s;
`BETWEEN` over three days → three paths; RLS bound + caller bound composed
into three explicit paths inside the guard.

### Knobs

All env-overridable; defaults come from measured production failure modes
and are mirrored in `.env.example`.

| Knob | Default | Meaning |
|---|---|---|
| `MAX_RESULT_ROWS` (alias `QUERY_TIMEOUT_ROWS`) | 200 | rows returned inline; above → summary shape |
| `MAX_RESULT_CHARS` | 10000 | inline JSON size cap; above → summary shape even if ≤ rows cap |
| `MAX_MATERIALIZE_ROWS` | 1000000 | LIMIT applied when materializing a result table |
| `RESULT_TABLE_LRU` | 100 | max materialized `_r_<n>` tables per session; least-recently-queried dropped |
| `SAMPLE_ROWS` | 3 | sample rows in a schema summary |
| `COMPRESSION_THRESHOLD` | 0.6 | fraction of the model context window (projected input tokens) that triggers stubbing |
| `MIN_MESSAGES_FOR_STUB` | 6 | never stub below this history length |
| `MAX_CONTEXT_MESSAGES` | 150 | message-count floor (Strands sliding window), secondary to the token lever |
| `MAX_EXPANDED_PARTITIONS` | 366 | upper bound of explicit paths the cost rewrite will expand a range into |
| `RAW_PATH_SCHEMES` | `s3://` | file-path schemes any principal may reference (table functions and replacement scans); case-insensitive |
| `DATAMAP_PROBE_PARTITION` | 2025-02-21 | partition used to DESCRIBE raw sources for the L1 index without a whole-prefix LIST |
| `DATAMAP_DESCRIBE_BUDGET_S` | 20 | time budget for gathering column names at first search |
| `AUDIT_LOG` / `AUDIT_METRIC_NAMESPACE` | 1 / `DataAgentOnDuckDB` | EMF audit lines on stdout → CloudWatch metrics |
| `OPS_PREFIX`, `OPS_CUR_SRC`, `OPS_CLOUDTRAIL_SRC`, `OPS_ACCOUNT_ID` | unset | cloud-ops scenario mounts and the account the RLS predicates pin to |
| `DUCKDB_MEMORY_LIMIT` | 2GB | engine envelope |

## Code organization: one package per layer, one statement pipeline

The five layers are the package layout, and the statement path is one
explicit pipeline instead of hand-wired copies. The point is that the next
layer (a semantic layer) and the next scenario are each one addition in one
place.

```
agent.py                      composition root: Agent + tools + conversation
                              manager, AgentCore app, entrypoint, CLI dispatch
data_agent/
  storage/                    legs.py (s3t/glue/dl mount + docs), registry.py,
                              scenarios/{base,onchain,cloudops}.py
  governance/                 policies.py, hardening.py, rewrite.py (authz only)
  context/                    results.py, compression.py, datamap.py, cost.py, prompt.py
  compute/                    engine.py (connection, mounts, lock, schema
                              prober), gate.py (read-only gate)
  scheduling/                 runtime.py (claims, envelopes, stream), cli.py
  pipeline.py                 Gate → Govern → Cost → Execute; run() / run_text()
  tools.py                    run_sql (pumps the pipeline), search_schema
  audit.py                    one EMF line per statement, session counters
```

- **Scenario contract** (`storage/scenarios/base.py`): one frozen dataclass —
  `views`, `sources` (cost-gate specs), `doc`, `policies`, `macros`,
  `lenient`. The registry merges every enabled scenario: the engine mounts
  their views (lenient ones skip-and-record a dataset that has not landed
  yet), `PARTITIONED_SOURCES` feeds the cost gate and the data map,
  `catalog_doc()` feeds the prompt, `default_policies()` feeds governance
  (the platform `ops:admin` role plus every scenario's personas).
- **Pipeline** (`pipeline.py`): stages share one interface — read/update a
  `Statement`, yield trace events, raise `Rejected` to stop. The driver
  audits exactly once per statement and emits the result string. The
  streaming tool pumps the generator one item at a time off the event loop
  (`asyncio.to_thread(next, gen)`), so the trace streams live while the
  engine call runs; the connection lock never spans a yield. A new layer is
  a new stage in `STAGES` — a test inserts a `semantic` stage and sees
  `gate → semantic → govern → cost → execute` through the real tool.
- **Dependency direction** is downward only: scheduling → pipeline →
  context, governance → compute → storage; audit is a leaf. The governance
  schema prober is wired in `pipeline.py` from `engine.probe_columns`, so
  compute never imports governance.
- **Test seams** follow the code: `engine.get_conn`,
  `governance.policies.POLICIES` / `DEFAULT_ROLE` (patch the module the
  functions read, not the facade re-export), `governance.hardening.
  RAW_PATH_SCHEMES`, `storage.legs.*`, `context.prompt.*`,
  `scheduling.runtime.map_event`, `pipeline.run_text`.

## Streaming trace (why the UI can show the engine live)

The entrypoint is an async generator, so AgentCore serves native SSE. Strands
events are wrapped in a small typed envelope
(`start | reasoning | text | tool_start | trace | result`); `run_sql` is an
async-generator tool that yields one trace event per pipeline stage —
`gate` (raw SQL), `govern` (principal, injected RLS predicates, hidden
columns, or a clean rejection), `cost` (sources, partition filter, expanded
paths, or a rejection), `execute` (engine, `engine_ms`, rows, shape,
materialized ref) — and the final `result` event carries the consolidated
trace array plus the session counters (statements, rejected, materialized,
compressed). The Streamlit UI renders the transcript in true arrival order
and totals engine time and counters on a Trace tab. One practical note:
tool-input deltas stream per character, so tool cards must coalesce events
by `tool_use_id`.

## Audit: one line per statement, metrics for free

`data_agent/audit.py` emits one structured JSON line per `run_sql` on stdout
in CloudWatch **Embedded Metric Format**: principal, SQL hash and text, what
governance and the cost policy did, shape, rows, engine time, materialized
ref, rejection reason and stage. The runtime's log group keeps the full
record and CloudWatch extracts the numeric fields as metrics (`Statements`,
`EngineMs`, `Rows`, `Materialized`, `ExpandedPaths`, dimensioned by
`Outcome` = executed / rejected / error) without a `PutMetricData` call or
any extra IAM permission. This is an audit *log*, not an audit store —
retention, search and alerting are CloudWatch's job. The same record feeds
the session counters the `result` envelope carries to the UI. When the
runtime log group is exported to S3, the audit lines are themselves
queryable through the cloud-ops scenario's `cw_logs` view.

## Scenarios (and why they fit the pattern)

A scenario is a data domain plugged into the storage layer. Both shipped
scenarios share one investigation shape — anchor on an identity, expand hop
by hop, keep the case working set in a temp table — which is the in-process
engine's sweet spot (build the working set once, then sub-millisecond
follow-ups) and a poor fit for a re-scan-per-query remote service.

### On-chain fund-flow investigation

Public Ethereum data (`s3://aws-public-blockchain`): an investigation is a
branching, multi-hop query tree — each query depends on the previous result,
over a case-scoped working set interrogated dozens of times. The attribution
tables are two small label sets: `ofac_addresses` (the real, public OFAC SDN
address list) and `cex_deposit_labels` (**synthetic** sample records whose
`subject_identity` column mimics the KYC linkage a real exchange would hold —
that column is exactly what the CLS policy protects). The agent, governance
and UI code are scenario-agnostic; the dataset, catalog doc and policy table
are the swap points.

### Cloud-ops investigation on the account's own exports

Same agent, same governance, enterprise semantics: the identity is an IAM
principal instead of a wallet, RLS scopes by account, CLS hides resource ids
or source IPs.

- **Sources** are exports the account already produces: Data Exports CUR 2.0
  (`…/BILLING_PERIOD=YYYY-MM/*.csv.gz`, hourly, resources included), the
  multi-region CloudTrail trail (`AWSLogs/<acct>/CloudTrail/<region>/YYYY/MM/
  DD/*.json.gz`), and a CloudWatch Logs export task to S3.
- **Two paths per source**, mirroring the on-chain scenario: a *raw* view
  over the native export as it lands (zero-ETL: `read_csv` over gzip,
  `read_json` over gzip with the `Records` array unnested) for the current
  period/day, and a *curated* Parquet copy written by the same engine
  (`scripts/load_ops_data.py`) under the solution bucket's `ops/` prefix,
  hive-partitioned by `billing_period` / `date`, typed and flattened
  (`userIdentity.*`, error fields, source IP, user agent; request/response
  kept as JSON strings). Views: `cur_line_items`, `cloudtrail_events`,
  `cw_logs` (curated) + `cur_raw`, `cloudtrail_raw(day, region)` (native).
  Partition columns are registered for the cost gate.
- **Governance with enterprise meaning**: `ops:finops` — RLS
  `line_item_usage_account_id = '<own account>'`, CLS hides
  `line_item_resource_id`; `ops:secops` — full CloudTrail rows for the own
  account; `ops:secops-junior` — CLS hides `source_ip_address` and
  `user_agent`. The account is pinned by `OPS_ACCOUNT_ID`; unset, the
  predicate can never match — fail closed, never "all accounts". The
  runtime role reads the two source buckets through scoped ARNs
  (`-c opsSourceBuckets=` at deploy time), and the solution bucket's policy
  grants the CloudWatch Logs service principal the two statements an export
  task needs.
- **Lesson paid for in the cloud**: DuckDB binds a view at CREATE, so a
  dataset that has not landed yet (the CloudWatch export before its first
  load) took the whole engine init down. Ops views are therefore mounted one
  by one (the scenario is `lenient`) and a failure is recorded in
  `OPS_VIEW_ERRORS` instead of raised.
- Curation with the in-process engine is fast enough not to need a job
  framework: about a million CUR rows per billing period in single-digit
  seconds; a region-day of CloudTrail (thousands of gzip files) in under a
  minute.
- Data boundary: the ops data is the operator's own account telemetry; the
  repository ships loaders and views, never data. The eval ground truth for
  this scenario is specific to the reference account and must be recomputed
  for yours.

## Evaluation: a black box with a glass pipeline

`tests/eval/cases/*.yaml` + `scripts/run_eval.py` run against real Bedrock
(manually or nightly, not as a CI gate). A case names a `category`, a
`persona`, one `prompt` or several `prompts` (multi-turn, optionally with a
`session_break`), `max_tool_calls`, and `expect`. Assertions are made on the
**trace events** the pipeline emits — gate / govern / cost / execute are
structured, so no output-text matching is needed for the mechanism claims:

| Category | Asserts |
|---|---|
| routing | the right view/leg/table appears in executed SQL |
| governance-adversarial | the proven bypass shapes → `rejected`; RLS predicate present; CLS column absent |
| cost | every raw scan carries a partition predicate; expansions happened |
| accuracy | known incident numbers (e.g. 401,346.77 ETH inbound) within tolerance |
| multi_turn | follow-ups hit `_r_` / temp tables; engine_ms small |
| ops | the second scenario's personas against CUR / CloudTrail ground truth |

Adversarial cases name their `probe_sql_regex`: a probe that executed fails
the case regardless of anything else; a model that refused to issue it is
defence in depth and is recorded as a note, not a failure. `--mode cloud`
logs in to Cognito as the persona user and calls the deployed runtime over
the JWT path exactly as the UI does; `--mode local` drives the agent
in-process. The first cloud runs were informative in the way an eval should
be: model refusals before the tool call, an uppercase `S3://` scheme, a
restricted role reaching for `read_parquet(path)` instead of the view — two
became rewrite-layer fixes, two became prompt rules. Results are written to
`docs/eval-results.md` / `.json` with AWS-id-shaped strings redacted.

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
- **Result read-back**: a materialized 1,322-row result read back seven
  times at 3.7–6.2 ms each, zero S3 re-scans (see the context layer above).
- **Access-path equivalence**: one session running the same aggregation over
  all four paths returns identical results, with per-path `engine_ms`.
- **Resource envelope**: with `DUCKDB_MEMORY_LIMIT=256MB` plus a spill
  directory, a full-day sort completes in seconds rather than failing.
- **Concurrency isolation**: `scripts/concurrent_invoke.sh N` runs N parallel
  sessions with independent latencies — no queue, no cross-talk.
- **Evaluation**: 35/35 cases across six categories on the reference
  deployment ([eval-results.md](eval-results.md)).

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
| Operational telemetry already exported to S3 (CUR, CloudTrail, CloudWatch Logs) | This pattern — read the export where it lands, curate to Parquet with the same engine, govern by account/column |
| Both (most real estates) | Hybrid: DuckDB fast path + warehouse MCP fallback |
| Working set approaches single-node memory | Split the query or move up the spectrum (Athena/Redshift) |
