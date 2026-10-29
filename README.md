# Data Agent on DuckDB

A reference implementation of a **governed data agent on Amazon Bedrock
AgentCore**: DuckDB is embedded inside the agent process and reads Amazon S3
Tables in place; row- and column-level governance is compiled into every SQL
statement before the engine runs it; lake data and non-lake sources meet in
one session working set. The architecture is laid out as six layers plus an
evaluation-and-operations layer around them, and every layer ships as code in
this repository. Point it at your own S3 tables and policies; data domains
plug in as scenarios (two ship with the repo).

It differs from the common shape of a data agent in three architecture
choices:

- **The engine is embedded in the agent process.** DuckDB runs inside the
  agent on [Amazon Bedrock AgentCore](https://aws.amazon.com/bedrock/agentcore/)
  Runtime, sized for the real workload — a trial-and-error conversation of
  short SQL statements.
- **Data stays on the lake.** Tables are read in place from
  [Amazon S3 Tables](https://aws.amazon.com/s3/features/tables/) over the
  Iceberg REST endpoint; nothing is copied out for the agent.
- **Governance comes before the engine.** Row/column authorization and the cost
  gate are compiled into each statement before it reaches the engine, with
  [AWS Lake Formation](https://aws.amazon.com/lake-formation/) as the policy
  authority.

Because of these three choices, the three problems an agent hits first on a
real lake — cost (scanning everything), accuracy (reading a truncated result
as the whole answer) and authorization (reading what the caller may not see) —
each land at **one deterministic code location**, not at a sentence in a
prompt.

> [!WARNING]
> This is a reference implementation, provided as is. Before production use,
> add monitoring/alerting, rate limiting, HA, fine-grained audit trails, and
> hardened credential rotation for your environment. The exchange/entity
> attribution labels shipped with the on-chain scenario are **synthetic sample
> data**, not real attribution (the OFAC sanctioned-address list is real and
> cited below).

## The workload: a conversation, not one perfect SQL

Watch an agent answer a business question end to end and four facts recur:

1. **One question is 5–10 short statements, not one.** DESCRIBE the table,
   sample it, get a column wrong, fix it, aggregate.
2. **Mistakes are the main path, not the exception.** A wrong column name, the
   engine's error, a rewrite and a re-run happen in almost every session — the
   architecture has to treat that loop as normal traffic.
3. **Results are often too large.** The model is either misled by a truncated
   result or drags a whole table into its context window.
4. **Sessions are bursty and short-lived.** They appear and disappear with the
   agent's invocations; there is no steady concurrency and no load curve to
   plan for.

All four point at the first choice: put the analytics engine inside the agent
process, next to the model — so trial and error is affordable and a session's
intermediate results have somewhere to live.

## Why the engine lives in the agent process

Three reasons come from the workload itself:

- **Analytical queries are small.** Fivetran's analysis of real warehouse
  workloads (September 2024) found that among queries scanning at least 1 MB,
  the median scans about 100 MB, and that 99.9% of real queries fit on a single
  large node ([source](https://www.fivetran.com/blog/how-do-people-use-snowflake-and-redshift)).
- **Fast start, fast stop.** An in-process engine is a library: no port, no
  cluster to provision or drain. It exists when the session does — the agent
  platform's session scheduling *is* the resource management, so there is no
  second query queue to run.
- **The process boundary is the isolation boundary.** One process holds one
  session's whole state. The alternative — many tenants on one shared cluster,
  separated logically — brings noisy neighbours and a wider blast radius;
  sessions here never queue behind or see each other.

The stronger argument is four mechanisms that **only hold when the engine is
local** (the context and compute layers below build on them):

| Mechanism | What it means here |
|---|---|
| **Oversized results are not truncated** | the whole result stays in the engine as a session table; the model gets a schema summary, a few samples and the read-back path |
| **History compresses into pointers** | an old result becomes one line pointing at an engine table that can be queried again |
| **The cost gate is a rewrite before the engine** | predicates are rewritten and partitions expanded into explicit paths *before* execution; a remote query service can only refuse after the fact, by bytes scanned |
| **Error feedback stays in-process** | a wrong statement costs a local round trip; the engine's error text goes straight back to the model and the repair loop never leaves the process |

To be precise about the alternative: a remote engine **can** keep session
state too — the Amazon Redshift Data API's `SessionKeepAliveSeconds` keeps temp
tables across calls. The difference is that every read-back still crosses the
network, and that sessions carry their own limits (one query at a time per
session, session caps per workgroup, a maximum session length).

In one line: an agent's data work is a trial-and-error conversation of short
SQL, so execution must sit close to the model; and object storage has no
query-time policy engine, so authorization must be compiled into each
statement before it reaches the engine. Embedding the engine in the agent
process satisfies both.

**When not to use it.** The data already lives in a governed warehouse (connect
to it directly), or a single question needs a multi-TB working set. The
envelope is declared rather than discovered the hard way: a statement that
expands past `UPGRADE_ADVICE_PATHS` partition paths (default 120, below the
hard cap) is refused **with a route** — a structured suggestion carrying the
equivalent governed SQL for Amazon Athena or Amazon Redshift over the same
lake; routing stays outside the agent. Comparison and decision table:
[docs/ab-results.md](docs/ab-results.md), [docs/design.md](docs/design.md).

| If you need… | Look at… |
|---|---|
| Business-user self-service Q&A over dashboards and datasets | Amazon Quick |
| Data-engineering agents inside Amazon SageMaker Unified Studio | SageMaker Data Agent |
| A coding agent operating your AWS data stack from the IDE | [Agent Toolkit for AWS](https://github.com/aws/agent-toolkit-for-aws), `aws-data-analytics` plugin |
| A BI / rendering engine, a catalog UI with lineage, or a multi-source adapter inside the database | out of scope — the agent returns results with their sources; rendering belongs to the front door |

## Architecture: six layers and a +1

<img src="docs/architecture.svg" alt="Architecture: the Strands agent, semantic, context and governance layers and embedded DuckDB inside one AgentCore Runtime session (one microVM); S3 Tables read in place over the Iceberg REST endpoint with the execution role; Lake Formation's per-caller filters compiled into the rewrite; non-lake sources through AgentCore Gateway with a Cedar policy, landed as owner-bound handles" width="100%">

<details>
<summary>Architecture (text version)</summary>

```
UI / CLI ──▶ AgentCore Runtime (HTTP)  ┐ 1 session = 1 microVM = 1 DuckDB
Coding agent ──▶ AgentCore Runtime (MCP)┘ inbound JWT authorizer → verified claims {tenant, role, sub}
  ├─ L1 Scheduling — session bound to its owner and role; SSE trace stream back to the caller
  ├─ Strands Agent + Bedrock Claude (writes SQL / calls metrics and verified queries)
  ├─ L2 Semantic   — verified queries → registered metrics → free SQL (Git assets: semantic/, verified_queries/)
  ├─ L3 Context    — data map · table wiki · cost gate · result shaping · history pointers · budgets
  ├─ L4 Governance — sqlglot RLS/CLS rewrite, fail-closed; Lake Formation filters fetched per caller
  └─ L5 Compute    — DuckDB in-process (baked extensions)
       ├─▶ L6 Storage: S3 Tables managed Iceberg over the Iceberg REST endpoint (execution role)
       │     + compatibility legs: raw Parquet / exports · Glue Iceberg REST · DuckLake
       └─◀ _r_<n> handles: query results AND rows fetched through AgentCore Gateway
            (labels API, warehouse) with the caller's token; Cedar policy on the Gateway
+1 Evaluation & operations: governance matrix + eval gate · EMF audit line per statement · feedback
```
</details>

| Layer | The question it answers | Key mechanisms | Code |
|---|---|---|---|
| **L1 Scheduling** | How are a session's state, cost and failures contained to that session? | 1 session = 1 microVM = 1 engine; HTTP and MCP entry points behind one inbound JWT authorizer; the session is bound to its owner and role | `data_agent/scheduling/` |
| **L2 Semantic** | How do reviewed definitions stop being re-invented by the model? | three-level lookup: verified queries → registered metrics → free SQL; definitions, table cards and wiki are open files in your Git | `data_agent/semantic/`, `semantic/`, `verified_queries/` |
| **L3 Context** | What does the model see, and what is the engine asked to do? | generated data map · cost gate · results never truncated · history as pointers · session budgets · error hints · injection marking | `data_agent/context/` |
| **L4 Governance** | Who may read which rows and columns of which table? | RLS/CLS compiled into every statement (sqlglot AST rewrite), fail-closed; Lake Formation as the policy authority | `data_agent/governance/` |
| **L5 Compute** | How do the lake and every other source meet in one analysis? | in-process DuckDB; the session working set; non-lake sources through AgentCore Gateway land as owner-bound handles — never ATTACHed | `data_agent/compute/`, `data_agent/tools.py` |
| **L6 Storage** | Where does the data live, and how is it read? | S3 Tables managed Apache Iceberg, read in place over the Iceberg REST endpoint | `data_agent/storage/` |
| **+1 Evaluation & operations** | How do we know it stays right after release? | governance matrix and eval in CI; online feedback bucketed by answer source; corrections return as reviewed changes | `scripts/run_eval.py`, `tests/`, `data_agent/feedback.py` |

The layers are responsibilities; in code, everything that touches one SQL
statement converges on **one statement pipeline**, shared by every entry point
(`data_agent/pipeline.py`):

```python
STAGES = [Gate(), Semantic(), Govern(), Cost(), Execute()]
```

**Data map** (L3, before the SQL exists) → **gate** (parseable, read-only, own
handles only) → **semantic compile** (L2) → **governance rewrite** (L4) →
**cost gate** (L3) → **engine** (L5). Every stage is deterministic code: any
stage can refuse, and then the statement does not run; every stage emits a
trace event and every statement leaves one audit line — the evaluation layer
asserts on exactly those events. Adding a check is adding a stage; the tools
and the UI do not change.

Two orders in that pipeline must never be swapped. **Semantic before
governance** — a compiled metric or a verified query is rewritten for the
caller's role like any other SQL, so no statement bypasses governance.
**Cost after governance** — the cost gate works on the rewritten statement:
the governance-injected row predicate and the caller's own time condition
combine into one closed range there before partitions are expanded; expanding
first would replace table references with raw paths, and governance would
find no table name to guard. The rule behind both: every step that produces or
transforms SQL runs before governance, so governance sees the shape it
protects — the one exception, the cost expansion, only substitutes the data
source inside references governance has already wrapped.

The agent framework is Strands Agents; nothing in the pipeline depends on it.

## L1 Scheduling — one session, one microVM, one person

AgentCore Runtime gives every session its own microVM: **1 session = 1 microVM
= 1 DuckDB engine**, created with the session and discarded at its end. That
isolation is the platform's. On top of it, this implementation binds the
session to the verified caller **and role**: if a different identity — or the
same user under a different role — presents the session id, the engine,
result handles, data map, history and audit counters are reset before anything
runs, so state computed under one policy never survives into another.

Authentication happens before agent code. The user signs in at the identity
provider (Amazon Cognito in the reference deployment; another OIDC provider's
claim shape is mapped with `CLAIM_TENANT_PATH` / `CLAIM_ROLE_PATH` /
`CLAIM_SUB_PATH`), and the Runtime's inbound JWT authorizer verifies the token's
signature, expiry and audience (`allowedAudience` — ID tokens carry `aud`, not
`client_id`) before the agent is called. The agent reads `{tenant, role, sub}`
from the verified token and **only authorizes** — the split the governance
layer is built on.

Two entry points share that contract:

| Entry | Mechanism |
|---|---|
| **Analysts** — web UI or CLI, over HTTP | Cognito ID token → Runtime `customJWTAuthorizer` → claims; the `Authorization` header must be on the runtime's [request header allowlist](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html). The reasoning and the pipeline trace stream back live |
| **Other agents** — e.g. a coding assistant in the IDE, over MCP | the same image on a second Runtime with `serverProtocol: MCP`, the same authorizer, the same tools (`run_sql`, `search_schema`, `call_metric`, `call_verified`, `list_sources`, `fetch_source`) and the same governance. Identity is the verified token only; a different principal on a reused connection resets the session. The client discovers where to log in from the 401's `WWW-Authenticate … resource_metadata` and uses a pre-registered client id (no Dynamic Client Registration). Inbound does not go through AgentCore Gateway: measured, an MCP Gateway cannot forward the caller's JWT to a Runtime target, so a Gateway hop would substitute the gateway's own identity — the Gateway is for outbound sources |

## L2 Semantic — stable definitions compiled to SQL, owned by you

Reviewed definitions must not be re-derived by the model on every question —
and the layer must not pretend every question has a ready answer. It is a
**three-level lookup**, from most deterministic to most free:

1. **Verified query** — the question itself has been reviewed: a natural-language
   question, reviewed SQL and typed parameters (`verified_queries/*.yaml`).
   `call_verified` runs it exactly as written; parameters are bound as literals
   in the parsed AST (DuckDB `$name` placeholders), so a value can never leave
   its quotes.
2. **Registered metric** — the definition has been reviewed, the phrasing has
   not ("cost per service this month"). The model matches the metric's
   description and calls `call_metric`; the compiler expands it into one SELECT,
   and dimensions, time windows and filters can only take allow-listed values.
3. **Free SQL** — neither matched, which is the common case in a real session.
   The model writes SQL within the data map and the table cards.

All three go through the same governance rewrite and cost gate — the semantic
layer is a governed shortcut, not a bypass. The model picks a level from
**catalogs filtered by its role** (a definition over a table the role cannot
read is never shown). Every statement's audit line records which level
produced it (`provenance` = `verified` | `metric` | `free_sql`); the
evaluation layer buckets accuracy by it.

**The assets belong to you.** Everything that defines meaning is an
open-format file in your repository, loaded fail-closed at cold start (a
malformed file refuses to load, like a malformed policy) and re-tested by the
`asset-gate` CI workflow on every change:

| Asset | File |
|---|---|
| Metric definitions (description and owner are required fields — the model selects by the description) | `semantic/<scenario>.yaml` |
| Verified queries | `verified_queries/<scenario>.yaml` |
| Table cards — grain, when to use, when not to, caveats, the reconciled column list | `semantic/cards/<scenario>.yaml` |
| Table wiki — business rules and the failure cases already paid for | `semantic/wiki/<table>.md` |
| Business glossary — the words users say, mapped to the tables involved | `semantic/glossary.yaml` |

Swap the agent or the model and these remain yours.

## L3 Context — seven gates, each closing a real failure mode

The context layer decides what the model sees and what the engine is asked to
do. Two of the opening problems — cost and accuracy — are solved here, and the
advantages of an in-process engine are realized (or thrown away) here. Each
gate below is a deterministic code location, not a prompt instruction.

<img src="docs/context-flow.svg" alt="Context layer: the data map before the SQL exists; semantic compile, governance rewrite and the cost gate before it runs; result shaping after it runs; history compression across turns" width="100%">

| Gate | Failure it closes | Mechanism | Knobs |
|---|---|---|---|
| **Cost gate** | one sentence scans the whole table | a raw partitioned source without a partition predicate is refused before execution; `=` / `IN` / `BETWEEN` literals are expanded into explicit per-partition paths (no prefix LIST); composes with governance's row bounds | `MAX_EXPANDED_PARTITIONS` 366 · `UPGRADE_ADVICE_PATHS` 120 |
| **Results never truncated** | the model reads a truncated result as the whole answer | results are materialized as `_r_<n>` — a server-minted handle bound to the session owner, with `created_at` and a stated lifetime; small results return inline, large ones return a schema summary, three labelled samples and the read-back path; a capped result always says it is capped, and a grouped metric that fills its limit says it is a top-N | `MAX_RESULT_ROWS` 200 · `MAX_MATERIALIZE_ROWS` 1,000,000 · `RESULT_TABLE_LRU` 100 |
| **History compression** | long sessions overflow the context | at 60% of the model window (measured from the last Bedrock usage), old materialized results become one line pointing at `_r_<n>`; superseded first, the current turn never touched | `COMPRESSION_THRESHOLD` 0.6 |
| **Generated data map** | hand-written schema docs drift; with many tables the model guesses | the L0 map is generated from the engine catalog at first invoke and filtered to the role; `search_schema` ranks tables with in-engine `fts` (BM25) over names, columns, cards and wiki, zero network; cards, prompt doc and engine schema are reconciled three ways | `DATAMAP_PROBE_PARTITION` |
| **Engine error hints** | one wrong column name costs several round trips | the engine's error text returns verbatim, plus a similar-name hint filtered by visibility (no denied object is revealed); an empty result under a row filter says "nothing you may see", not "no such data" | — |
| **Session budget** | one session's cost is unbounded | statements, expanded paths and engine time per session; past a budget every statement is refused and audited (on in the reference deployment) | `SESSION_MAX_STATEMENTS` · `SESSION_MAX_EXPANDED_PATHS` · `SESSION_MAX_ENGINE_MS` |
| **Content marking** | text inside a table is followed as an instruction | every text cell reaches the model wrapped as `data:«…»`, with the rule that row content is data, never instructions | — |

The cost gate in practice: a raw source is partitioned by day on S3; a
statement with no date condition would read every file, so it is refused before
the engine starts and the error names the missing condition. With
`date = '2025-02-21'` the gate rewrites it to read that day's files; with a
three-day `BETWEEN` it reads those three days. Iceberg tables do not need this
gate (the snapshot's manifest decides the files); the session budget backstops
every path.

## L4 Governance — governance is a rewrite, Lake Formation is the policy authority

**Authorization cannot be pushed into object storage.** Amazon S3 issues
credentials per *object*, and one Parquet or Iceberg data file holds every
tenant's rows and every column — a credential can limit which objects you may
fetch, never which rows inside them. Row/column filtering must be executed by
some compute that reads whole objects; the only question is which compute you
trust. This implementation puts it at the agent-side SQL compile step.

**The enforcement point** is a deterministic AST rewrite of every statement
(sqlglot, not the model): each governed table reference becomes
`(SELECT * EXCLUDE (denied_columns) FROM table WHERE row_filter) AS alias`.
Column denial probes the table's real schema and intersects; a failed probe
keeps every denial. A model hallucination can make an answer wrong, but it
cannot widen access — any SQL the model writes (hallucinated, malicious, or
steered by injected text) goes through the same rewrite, and what cannot be
parsed does not run.

| | Where | Contract |
|---|---|---|
| **AuthN** | AgentCore inbound JWT authorizer + Amazon Cognito | tokens validated before agent code runs; the ID token's `custom:tenant` / `custom:role` are the only identity input (the app client cannot write them — no self-assigned roles) |
| **AuthZ** | `data_agent/governance/` | principal → role → policy; RLS predicates ANDed in, denied columns `EXCLUDE`d; selecting a denied column explicitly is a clean refusal naming column and role |
| **Policy** | **AWS Lake Formation** when present (`LF_GOVERNANCE`); `GOVERNANCE_POLICIES` (JSON env) otherwise | validated at startup — a typo fails the process |

**Completeness.** The rewrite covers every statement shape; each rule below
closed a bypass that was proven executable against this code:

| Rule | Closes |
|---|---|
| a restricted role may reference only the tables its policy names (plus its own session tables and statement-local names) | a mounted-but-unlisted table — another scenario's, or the runtime's own `cw_logs` — read ungoverned |
| statement shapes are an allowlist on the parsed root | `EXPLAIN ANALYZE` and other opaque commands passing through, or leaking denied-row cardinality in a plan |
| a local name may not shadow a governed table | `WITH cex_deposit_labels AS (…)` unguarding the real table |
| no identity or no policy means the built-in `deny-all` floor | a missing claim becoming the admin role |
| DML behind a CTE prefix is rejected for every role | `WITH x AS (SELECT 1) DELETE …` |
| host-reaching functions and non-`s3://` paths are refused for every role | `read_text('/proc/self/environ')`, `FROM '/etc/passwd'` |

### Lake Formation as the policy authority

The rewrite answers *where* policy is enforced; Lake Formation answers *where
it is managed*. This follows Lake Formation's distributed-enforcement model
for external engines — **Lake Formation manages the policy, the engine
enforces it**; the rewrite is the engine-side enforcement:

- the caller's role maps to a data access IAM role; at session bind the agent
  assumes it with the authorized session tag (`LakeFormationAuthorizedCaller`)
  — the standard external-engine integration, enabled per account with an
  allowlist of callers and session tags;
- as that principal it calls `glue:GetUnfilteredTableMetadata`; Lake Formation
  **evaluates per caller** and returns the authorized columns and the row
  filter (PartiQL) — exactly what the rewrite compiles;
- the engine enforces, **fail-closed**: a table whose policy cannot be fetched
  is refused, never served under an older or weaker policy. The env policy
  remains the merged lower bound (stricter wins) and the configuration for
  deployments without Lake Formation. The data path is unchanged — the engine
  reads S3 Tables with the runtime's execution role.

Setup is one declarative file and one idempotent command:
`infra/lakeformation.yaml` (governed tables, persona → data access role, each
persona's data cells filter) and `scripts/setup_lakeformation.py` (dry-run by
default, `--apply` to converge, additive unless `--prune`; prints the
`LF_GOVERNANCE` value for the runtime).

**Consistency is tested, not assumed.** `scripts/lf_idempotence.py` asks ten
questions per persona twice — through this engine with Lake Formation's
filters compiled into the statement, and through Amazon Athena with Lake
Formation enforcing natively, as the same principal — and compares row for
row: **20/20**, with a negative control that turns red by 1.2 million rows if
the compilation is removed.

### Governance coverage matrix

"Total" is measured, not asserted. Every persona × every access path × every
statement shape is one statement run through the real pipeline on a local
engine shaped like the deployed catalog, with the expected outcome **derived
from the policy** — unrestricted passes unchanged, a ruled table gets its row
filter / column exclusion and executes, an unlisted table is refused before
the engine. Zero network, zero tokens; the cells run as unit tests in CI, and a
new governed table or persona adds cells with nothing to edit.

| Persona | raw | s3t | glue | dl | mcp | tool | unlisted table | foreign handle |
|---|---|---|---|---|---|---|---|---|
| admin | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | — | refused |
| investigator | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | refused | refused |
| junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | refused | refused |
| finops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | refused | refused |
| secops | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | refused | refused |
| secops-junior | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | refused | refused |

6 personas × 6 paths × 5 shapes (plain, CTE, set operation, derived-table
alias, `CREATE TEMP TABLE … AS`) = 180 cells + 5 unlisted-table + 6
foreign-handle + 5 source-denied cells, **196/196**. The `mcp` path takes the
role from a token rather than a fixture; the `tool` path JOINs a tool result
(an owner-bound, `data:«…»`-marked handle) with the persona's governed table,
which keeps exactly the rewrite the direct path gives. Report:
[docs/governance-matrix.md](docs/governance-matrix.md); `python
scripts/run_matrix.py --all-paths --with-mcp --with-tool` regenerates it.
Threat model: [`docs/threat-model/`](docs/threat-model/).

## L5 Compute — one engine for the lake and every other source

Real analysis rarely uses only the lake: a reference figure lives in a
warehouse, a sanctions list or an FX rate comes from an HTTP API. The agent
must not become an ETL tool for them, and must not hold their credentials. So
two concerns are separated:

- **Integration and authorization belong to AgentCore Gateway.** Each source is
  a Gateway target exposed to the agent as an MCP tool; the source credential
  lives in the Gateway / token vault and is applied per request.
- **Computation comes back to the in-process engine.** A tool's rows are
  materialized as a session table and JOINed with lake tables in ordinary SQL.

The in-process engine is the session's single compute plane: query results
and tool results are both `_r_<n>` handles — server-minted, bound to the
caller, timestamped; a handle minted by another principal is refused before
the pipeline. "Fetch a subset, then reason over it" never re-scans the source.

`fetch_source` → (the **caller's own** token) → AgentCore Gateway → the source
(its credential never leaves the Gateway) → rows land as `_r_<n>` → JOIN with
the lake, in the same process:

```sql
-- the labels source's rows already landed as _r_1
SELECT t.to_address, sum(t.value) / 1e18 AS eth, l.sanctioned, l.exchange
FROM   s3t.blockchain.eth_traces AS t
JOIN   _r_1 AS l ON lower(t.to_address) = lower(l.address)
WHERE  t.date = '2025-02-21'
GROUP  BY ALL
ORDER  BY eth DESC;
```

The agent process holds no source credential — by design, since any code in
the microVM can read the execution role's credentials through the metadata
endpoint ([AgentCore documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/security-credentials-management.html)).

Outbound permission has three levels, each answering one question:

| Level | Question | Mechanism |
|---|---|---|
| **Deployment** | which sources can this deployment reach? | the Gateway's target list, each target with its own least-privilege credential; a system not on the list is not even a tool the model can see |
| **Persona** | which source tools may this role call? | two enforcement points from **one definition** — the persona policy's `sources` allowlist: the agent hides and refuses unlisted sources before any outbound call, and the Gateway's Cedar policy (rendered from the same field by `scripts/setup_gateway_policy.py`, attached in ENFORCE) refuses the same call even if the agent were bypassed |
| **User** | whose permissions does the source apply? | set by the target's credential type: with 3LO, on-behalf-of token exchange or IAM Identity Center trusted identity propagation, the source authorizes the real user; with a service credential (API key, the gateway's IAM role) the Gateway reads as a service identity and the audit line is marked `degraded=2lo`. Rows from a 2LO source are not re-filtered afterwards, so such a source may only serve data every role allowed to call it may see — otherwise it should not be connected |

| Source shape | Route | Status |
|---|---|---|
| Open-format data on S3 (Iceberg via S3 Tables / Glue, Parquet, DuckLake) | read in place by the engine (L6) | shipped |
| Governed stores, SaaS and HTTP APIs | a read-only Gateway tool with the caller's identity; rows land as `_r_<n>` and JOIN in-process; never ATTACHed into the engine | shipped for an HTTP labels/sanctions API; an Amazon Redshift Serverless target is in `infra/` (`-c withSources=true`) |
| Second-level freshness, or writes | a tool plus human review, outside the agent | out of scope |

Two hard rules hold on every path: **the SQL layer never issues HTTP** (raw
paths are allow-listed to `s3://`; `INSTALL` / `ATTACH` / `COPY` never pass the
gate), and **only DuckDB core extensions are loaded, none installed at
runtime** (`httpfs`, `aws`, `iceberg`, `ducklake`, `fts` are baked into the
image; `autoinstall_known_extensions=false`).

## L6 Storage — S3 Tables managed Iceberg, read in place with one ATTACH

How fast an engine reads depends largely on how the files are organized: small
files and growing snapshot metadata slow every engine, and an agent re-opens
the same files session after session. Amazon S3 Tables makes table maintenance
the platform's job — automatic compaction, snapshot management and
unreferenced-file removal, each configurable per table
([maintenance](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html)).

The integration with DuckDB is native: load `aws`, `httpfs` and `iceberg`
(pre-installed in the image), then one ATTACH:

```sql
ATTACH 'arn:aws:s3tables:<region>:<account>:bucket/<table-bucket>'
    AS s3t (TYPE iceberg, ENDPOINT_TYPE s3_tables, READ_ONLY);
SELECT count(*) FROM s3t.blockchain.eth_traces;
```

The same ATTACH works in both directions — the sample data is written into S3
Tables by DuckDB itself with `CREATE TABLE AS SELECT` (no Glue job, no Athena);
the agent mounts it read-only.

**Does reading in place drag the lake across the network?** No — what crosses
the network is narrowed four ways, all of them the engine deciding *what to
request* from metadata, and storage returning exactly the bytes requested
(measured on the deployed runtime; method in [docs/design.md](docs/design.md)):

1. **Files** — the snapshot's manifest lists exactly the data files of this
   table version (with row counts and other statistics); nothing is listed or
   scanned by prefix, and some aggregates are answered from the manifest alone
   — a whole-table `count(*)` moved **0** data bytes.
2. **Columns** — a column is a contiguous byte range inside a columnar file, so
   selecting columns does not require splitting files: one narrow column over
   the same rows ≈ 18 MB, adding two wide string columns ≈ 123 MB.
3. **Bytes** — HTTP range reads fetch only the needed ranges of a file.
4. **Partitions** — with a partition-key condition, rows are pre-filtered a
   whole partition at a time and files outside it are never requested (on raw
   partitioned sources the cost gate enforces this); conditions on other
   columns are evaluated by the engine over the columns it fetched.

**Access paths — one recommended, three compatibility.** Every extra path is
abstraction to carry; the other three stay because real lakes are shaped
differently, and the coverage matrix governs all four identically.

| Tier | Path | Access method | Alias | Env toggle |
|---|---|---|---|---|
| **recommended** | **B** | **S3 Tables (Iceberg REST)** — 1 REST GET + 7 GETs, no LIST | `s3t.*` | `S3_TABLES_ARN` |
| compatibility | A | raw Parquet / native exports (the cost gate expands partitions) | views | always on |
| compatibility | C | Glue Data Catalog (Iceberg REST), federated `s3tablescatalog` — the same table as B | `glue.*` | `GLUE_CATALOG` |
| compatibility | D | DuckLake (experimental) | `dl.*` | `DUCKLAKE_CATALOG` |

All four returned identical results on the incident slice — 3,492,205 rows over
the three-day `eth_traces` window, 1,226,888 on 2025-02-21 — and the
governance rewrite is asserted on every one.

## +1 Evaluation and operations — keeping it right after release

This kind of system decays silently: schemas and definitions change, and a
question answered correctly at launch can be answered wrong weeks later. So
evaluation is a standing layer, closed into a loop — **the offline half guards
every change, the online half watches for decay**.

**Offline, on every build.** An eval case is declarative — persona, prompt,
expectations (`tests/eval/cases/*.yaml`). The runner logs in as that persona and
calls the deployed runtime exactly as the UI does, so it measures the
production shape; it asserts on the streamed trace events — which SQL ran,
which policy applied, which stage refused — not on the answer's wording.
Governance cases name a probe statement: if it executed, the case fails
however good the answer. Golden answers are **computed at eval time** through
the same pipeline instead of hard-coded, and the governance matrix re-runs on
every build. A bandwidth gate fails the build if any category drops below its
locked baseline; the `ci` workflow (lint, unit + matrix, bandit, semgrep,
detect-secrets) runs on every push.

**Online, after release.** Every answer can be rated — 👍 / 👎 in the UI, or
`{"feedback": {"verdict", "turn_id", "correction"}}` on the HTTP entrypoint
(never sent to the model). A verdict is one audit line bucketed by the answer's
source — verified query, registered metric, or model-written SQL — so the
CloudWatch dashboard shows the **down-vote rate per answer source**, and the
offline report buckets accuracy the same way: which bucket decays first is
visible at a glance. Corrections become **reviewed changes**, never automatic
ones: `scripts/harvest_corrections.py` turns down-votes into a review bundle
(the question, the source, where to look first, the correction quoted as
untrusted text, a draft regression case whose expectation the reviewer writes)
for a pull request that must pass the asset gate.

The acceptance principle of this layer: **a right answer is not enough — the
path that produced it is accepted separately.** Current numbers are in
[Evaluation and observability](#evaluation-and-observability).

## Scenarios

A scenario is a data domain plugged into the storage layer: its views over S3,
the partition columns the cost gate must see, a prompt section, and the personas
it ships as default policies, its table cards, and the metrics it registers
for the semantic layer. Two are included; a third domain plugs in the same
way — one module under `data_agent/storage/scenarios/` and one registry entry.

### Scenario 1 — on-chain fund-flow investigation

**Data.** The AWS Public Blockchain dataset on `s3://aws-public-blockchain`
(Ethereum blocks, transactions, token transfers, traces, logs; hive-partitioned
by day), around a real, publicly reported incident. Two small label tables give
the ledger attribution: the real OFAC sanctioned-address list and a synthetic set
of exchange deposit-address labels. Raw Parquet is one access path; the incident
slice is also loaded into S3 Tables, Glue and DuckLake so every active path
answers the same questions.

**Method.** Anchor on a wallet address; follow the money through `eth_traces`
(internal calls, the only place a contract-mediated drain is visible): the
inbound drain, then the outbound fan-out, then cash-out attribution by joining
recipients against the label tables. The working set is built once as a temp
table and every follow-up runs against it in memory.

**Personas.** `exchange-a:investigator` sees the case's date window on the
ledger and only its own exchange's deposit labels; `exchange-a:junior` has the
same rows but `subject_identity` (customer PII linkage) is column-denied.

**Ground truth checked.** 401,346.77 ETH inbound · 40 × 10,000 ETH fan-out ·
8 OFAC hits · 2 cash-out hits.

**Using it.** Log in to the Streamlit UI as a persona and follow the sidebar's
shared investigation script — the same questions for every persona, so switching
users shows exactly where governance changes the outcome.

![Investigation transcript: narration interleaved with live SQL tool cards](docs/images/ui-investigation.png)

- Each `run_sql` is a card with the SQL and its `gate → govern → cost → execute`
  line; execute is badged with the in-process engine time and, for oversized
  results, the `_r_<n>` table the model was pointed to.
- After the working set is built, follow-ups run at single-digit milliseconds
  with zero S3 re-scans.
- The **Trace** tab reconstructs every statement and totals the session:
  statements, rejections, materialized results, compression stubs, engine time.
- Switch persona (log out, log in — identity is a validated JWT claim, not a UI
  toggle) and run the same permission-test question: the junior role gets a
  clean policy rejection for the `subject_identity` column.

![Tool card: in-process DuckDB executing against the in-memory working set in milliseconds](docs/images/ui-tool-card.png)
![Governance rejection: the junior persona's denied-column query rejected by policy](docs/images/ui-governance-rejection.png)

### Scenario 2 — cloud-ops investigation on the account's own AWS exports

**Data.** Three exports the account already produces on S3: the AWS Cost and
Usage Report 2.0, the CloudTrail trail, and a CloudWatch Logs export. DuckDB
itself curates them to partitioned Parquet (`scripts/load_ops_data.py` — no Glue
job, no Spark), and the native files stay readable in place: `cur_raw` reads the
gzip CSV export as delivered, `cloudtrail_raw(day, region)` reads one region-day
of gzip JSON.

**Method.** The same investigation shape with an IAM principal, access key or
resource id as the anchor: AssumeRole events → the session issuer → the role's
calls → errors → source IPs → cost by resource, hop by hop, with the working set
kept in a temp table. Cost questions aggregate hourly CUR line items by billing
period; a `billing_period` or `date` predicate is required, and the cost gate
expands it into the exact partitions.

**Personas.** `ops:finops` sees only its own account's line items and never
`line_item_resource_id`; `ops:secops` sees the account's CloudTrail events with
every column; `ops:secops-junior` sees the same events without
`source_ip_address` and `user_agent`. The account is pinned by `OPS_ACCOUNT_ID`;
unset, the predicate matches nothing — fail closed, never "all accounts".

**Ground truth checked** (reference deployment — recompute for your own account
with DuckDB on the curated Parquet). 2026-08 unblended cost 540.80 USD (AmazonEC2
305.59) · 103,631 CloudTrail events on 2026-09-02 across three regions · 13,858
AssumeRole events, with one workload role as the top principal.

**Using it.** The sidebar script's questions 6–9 are this scenario. Log in as
`finops-a` and ask for the month's cost by service: the tool card shows the row
filter on the account, the hidden `line_item_resource_id` column and the cost
gate turning `billing_period = '2026-08'` into one explicit partition path; the
engine answers in well under a second.

![finops tool card: RLS on the account, resource-id column hidden, cost gate expanded to one path, in-process execution](docs/images/ui-ops-finops-cost-card.png)
![finops answer: top services by unblended cost for the month with key findings](docs/images/ui-ops-finops-cost-answer.png)

Ask for the resource ids behind the EC2 cost and the drill-down comes back by
usage type and product family instead — the column is hidden by policy, and any
statement that names a hidden column is a clean rejection naming column and
role. Log in as `sec-jr` and ask which principals issued the most calls and from
which IPs: the first statement selects `source_ip_address` and is rejected by
policy; the model's fallback `DESCRIBE` on the raw view is refused by the cost
gate for lacking a `date` filter; it then re-scopes to the day and answers with
principals and error codes but no IPs.

![secops-junior: policy rejection for source_ip_address, then the cost gate refusing a DESCRIBE without a partition filter](docs/images/ui-ops-secops-junior-ips-hidden.png)

The **Trace** tab totals the session — statements, gate rejections, materialized
results, compression stubs, engine time:

![Trace tab: session totals for the cloud-ops session](docs/images/ui-ops-trace-tab.png)

## Try it from the terminal

Everything below runs against the deployed runtimes with a real persona login —
no UI, no notebook. `scripts/demo_env.sh` resolves the runtime ARNs, the gateway
URL and the two secrets (Cognito app-client secret, persona password from SSM
Parameter Store) into environment variables and prints nothing sensitive.

```bash
export COGNITO_POOL_ID=<pool id>  COGNITO_CLIENT_ID=<app client id>
source scripts/demo_env.sh
python scripts/agent_ask.py --list                      # the six personas
```

**Ask a question and watch the pipeline.** `agent_ask.py` prints the streamed
trace next to the answer — the gate's SQL, the RLS predicate and excluded
columns for THAT persona, the cost gate's expanded paths, engine ms, and the
`_r_<n>` handle when one is minted.

```bash
python scripts/agent_ask.py finops \
  "For billing_period '2026-08', give me the three costliest services using the registered metric."
python scripts/agent_ask.py junior \
  "List every deposit label row you can see, including subject_identity."   # clean CLS refusal
python scripts/agent_ask.py admin \
  "Run exactly once and quote the reply verbatim: SELECT user_agent FROM cur_line_items WHERE billing_period = '2026-08' LIMIT 3"
#   → the engine's Binder Error verbatim, then: Hint: column `user_agent` exists on: cloudtrail_events.
#   (as `secops`, who may not read that table, the same query gets NO hint — nothing leaks)
```

**The MCP surface, as a coding agent sees it** (`scripts/mcp_invoke.py`): six
read-only tools (`run_sql`, `search_schema`, `call_metric`, `call_verified`,
`list_sources`, `fetch_source`), two resources, and tool calls on one session — a handle minted
by the first call is readable by the second.

```bash
python scripts/mcp_invoke.py admin                       # tools + the context resource
python scripts/mcp_invoke.py junior \
  --call 'run_sql {"sql": "SELECT address, subject_identity FROM cex_deposit_labels"}'
python scripts/mcp_invoke.py finops \
  --call 'fetch_source {"tool": "redshift___cost_total", "args": {"billing_period": "2026-08"}}' \
  --call 'run_sql {"sql": "SELECT * FROM _r_1"}'         # the handle the fetch minted
python scripts/mcp_invoke.py admin --resource data-agent://session
```

**Install it in your own coding agent (the way to use it daily).** Register the
local stdio proxy once; it mints the token itself and refreshes it, so nothing
expires in your face and no credential is written to a config file:

```bash
claude mcp add --scope user data-agent -- \
  /abs/path/to/.venv/bin/python /abs/path/to/scripts/mcp_stdio_proxy.py --persona finops
claude mcp get data-agent          # ✔ Connected
```

Then, from any directory: *"ask data-agent for the three most expensive services in 2026-08"*. The proxy
resolves the runtime, pool and app client from the deployment itself, reads the
persona password from SSM Parameter Store, keeps ONE upstream MCP session (so
`_r_<n>` handles stay readable between calls) and reconnects with a fresh token
when the old one is refused. `--persona` decides which governance role you get:
`finops` sees the CUR tables with resource ids hidden, `secops` sees CloudTrail,
`junior` loses the customer-identity column. Remove it with
`claude mcp remove data-agent -s user`.

For a one-off session instead, `scripts/claude_code_mcp.sh <persona> <dir>`
writes a project-scoped `.mcp.json` with a 60-minute token (chmod 600, never
commit it). A client's automatic OAuth flow is not usable against this
deployment: the pool offers no Dynamic Client Registration and the authorizer
needs the ID token's `aud` plus the `custom:tenant` / `custom:role` claims,
which an access token does not carry.

**The outbound sources and their identity boundary** (`scripts/gateway_invoke.py`):
without a token the Gateway answers 401 with the RFC 9728 pointer to its
authorization server; with a persona's token it lists and runs the source tools.
The `redshift___*` examples need the optional warehouse (`-c withSources=true`
plus `scripts/redshift_load.py`); a deployment without it exposes the labels
source only, and `demo_env.sh` sets `EVAL_DEPLOYED_SOURCES` from the live target
list so the eval skips the warehouse case instead of failing it.

```bash
python scripts/gateway_invoke.py --no-token              # the 401 challenge + discovery doc
python scripts/gateway_invoke.py finops --call 'redshift___cost_total {"billing_period": "2026-08"}'
python scripts/agent_ask.py finops \
  "Use the redshift cost_total source for 2026-08, then the same aggregate on the lake, and say whether they agree."
```

**Offline, free, instant** — no tokens, no network:

```bash
./.venv/bin/python -m pytest tests/unit -q               # the whole suite
python scripts/run_matrix.py --all-paths --with-mcp --with-tool   # governance matrix → docs/governance-matrix.md
python scripts/reconcile.py                              # doc ↔ card ↔ engine (needs S3 reach)
python scripts/run_eval.py --mode cloud --category semantic   # one eval category
```

## Evaluation and observability

How the layer works is described under [+1 Evaluation and operations](#1-evaluation-and-operations--keeping-it-right-after-release); this section holds the current numbers and how to run it.

Latest full cloud run: **57/57**, every golden recomputed in-region
(2026-10-29) — accuracy by answer source: verified
1/1, metric 7/7, free SQL 29/29, refusals and text-only 20/20. Governance carries one
more check of its own: `scripts/lf_idempotence.py` asks ten questions per
persona twice — once through this engine with Lake Formation's filters
compiled into the statement, once through Athena with Lake Formation enforcing
natively, as the same principal — and compares row for row (**20/20**, with a
negative control that goes red by 1.2 million rows if the compilation is
removed).

| Category | Cases | Pass | What it proves |
|---|---|---|---|
| routing | 5 | 5 | the right access path and table for the question |
| governance-adversarial | 8 | 8 | RLS/CLS applied through every path; bypass shapes stopped |
| cost | 7 | 7 | unbounded raw scans refused; literal filters expanded to explicit paths; past the envelope, a route instead of an OOM |
| accuracy | 6 | 6 | incident and cost ground truth reproduced; a wrong column name repaired from the error |
| multi-turn | 2 | 2 | working-set reuse and `_r_<n>` read-back across turns |
| ops | 13 | 13 | the second scenario: finops/secops personas, CUR and CloudTrail ground truth, session budgets |
| semantic | 9 | 9 | registered metrics compiled and governed; a verified query run as reviewed; unregistered metric, dimension and parameter injection and a denied column refused |
| injection | 3 | 3 | an instruction planted in a label row, a CloudTrail field and a tool result is reported, never followed |
| sources | 4 | 4 | non-lake rows fetched with the caller's identity and joined in-process; a source outside the role's allowlist refused before the call |

Full results per case: [docs/eval-results-2026-10-29.md](docs/eval-results-2026-10-29.md)
(the earlier run with per-case traces: [docs/eval-results.md](docs/eval-results.md)).

```bash
AGENT_RUNTIME_ARN=<runtime arn> COGNITO_CLIENT_ID=<id> COGNITO_CLIENT_SECRET=<secret> \
EVAL_PASSWORD=<persona password> ./.venv/bin/python scripts/run_eval.py --mode cloud
```

Every `run_sql` also emits one CloudWatch Embedded-Metric-Format line
(`data_agent/audit.py`) — principal, SQL, governance and cost decisions, shape,
rows, engine time, bytes scanned and S3 request count (from the engine's own
profiling and HTTP log, zero network; a working-set read-back correctly reports
0 requests) — so the runtime log group yields metrics with no metrics API
call.

## Prerequisites

- Python 3.11–3.13 (3.14 lacks prebuilt duckdb wheels)
- An AWS account with Amazon Bedrock model access (Claude) in your region
- AWS CDK v2 (`npm i -g aws-cdk`) for infrastructure deployment
- AWS credentials configured (`aws sts get-caller-identity` works)

## Deploy

```bash
# 1. Infrastructure (S3 Tables bucket, DuckLake bucket, Cognito pool, exec role, logs)
cd infra && pip install -r requirements.txt && cdk deploy
# outputs: TableBucketArn, GlueCatalogId, DuckLakeCatalog, LabelsPrefix, OpsPrefix,
#          ExecutionRoleArn, CognitoDiscoveryUrl, CognitoAudience

# 2. Load the sample data (DuckDB itself writes the Iceberg table — no Spark/Glue job)
#    Run on AWS-side compute for speed (reads the public us-east-2 dataset over the backbone).
cd .. && python3.13 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/load_s3tables.py \
    --table-bucket-arn <TableBucketArn> --ducklake <DuckLakeCatalog> \
    --labels-prefix <LabelsPrefix> --days 2025-02-21:2025-02-23

# 2b. (optional) Cloud-ops scenario — curate your own exports to Parquet with DuckDB.
#     Deploy the stack with -c opsSourceBuckets=<cur-bucket>,<cloudtrail-bucket> so the
#     runtime role may read them; run this on AWS-side compute.
./.venv/bin/python scripts/load_ops_data.py \
    --cur-src s3://<cur-bucket>/<prefix>/<export>/data \
    --cloudtrail-src s3://<trail-bucket>/AWSLogs/<account>/CloudTrail \
    --cwlogs-src s3://<solution-bucket>/ops/cwlogs-export \
    --out <OpsPrefix> --periods 2026-07,2026-08,2026-09 \
    --regions us-west-2,us-east-1 --days 2026-08-21:2026-09-03

# 3. Agent runtime (configure the inbound JWT authorizer from the Cognito outputs)
./.venv/bin/pip install bedrock-agentcore-starter-toolkit
./.venv/bin/agentcore configure --entrypoint agent.py --name duckdb_analyst \
    --execution-role <ExecutionRoleArn> --region <your-region> --non-interactive \
    --authorizer-config '{"customJWTAuthorizer":{"discoveryUrl":"<CognitoDiscoveryUrl>","allowedAudience":["<CognitoAudience>"]}}' \
    --request-header-allowlist Authorization
./.venv/bin/agentcore launch --env S3_TABLES_ARN=<TableBucketArn> \
    --env GLUE_CATALOG=<GlueCatalogId> --env DUCKLAKE_CATALOG=<DuckLakeCatalog> \
    --env LABELS_PREFIX=<LabelsPrefix> \
    --env OPS_PREFIX=<OpsPrefix> --env OPS_CUR_SRC=s3://<cur-bucket>/<prefix>/<export>/data \
    --env OPS_CLOUDTRAIL_SRC=s3://<trail-bucket>/AWSLogs/<account>/CloudTrail \
    --env OPS_ACCOUNT_ID=<account>            # the last four only for the ops scenario

#    Two more variables label the deployment for the audit line and point the
#    outbound client at the gateway (step 5): --env AGENT_IDENTITY=duckdb_analyst
#    --env GATEWAY_MCP_URL=<gateway url>. Pass EVERY env on every launch — the
#    toolkit replaces the set, it does not merge.

# 4. Chat UI (Cognito login → streamed reasoning + live gate→govern→cost→execute trace)
#    AGENT_RUNTIME_ARN may be omitted when running from the repo: the UI falls back to the
#    ARN that `agentcore launch` recorded in .bedrock_agentcore.yaml.
cd ui && pip install -r requirements.txt
AGENT_RUNTIME_ARN=<runtime arn> COGNITO_CLIENT_ID=<id> COGNITO_CLIENT_SECRET=<secret> \
    AGENT_REGION=<your-region> streamlit run app.py

# 5. (optional) The MCP surface for coding agents — a SECOND runtime on the same image,
#    protocol MCP, same authorizer. Not a CloudFormation resource yet; one control-plane call:
aws bedrock-agentcore-control create-agent-runtime --agent-runtime-name duckdb_analyst_mcp \
    --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<image uri from agentcore status>"}}' \
    --role-arn <ExecutionRoleArn> --network-configuration '{"networkMode":"PUBLIC"}' \
    --protocol-configuration '{"serverProtocol":"MCP"}' \
    --authorizer-configuration '{"customJWTAuthorizer":{"discoveryUrl":"<CognitoDiscoveryUrl>","allowedAudience":["<CognitoAudience>"]}}' \
    --request-header-configuration '{"requestHeaderAllowlist":["Authorization"]}' \
    --environment-variables '{<the same variables as step 3>, "AGENT_SURFACE":"mcp", "AGENT_IDENTITY":"duckdb_analyst_mcp"}'
#    Then register it in your coding agent: see "Install it in your own coding agent" above.

# 6. (optional) Non-lake sources through AgentCore Gateway. The CDK stack deploys the
#    gateway role, the two source Lambdas (labels API, Redshift) and Redshift Serverless
#    (-c withSources=true; the warehouse is loaded with scripts/redshift_load.py). The
#    Gateway itself and its targets are control-plane calls (not CloudFormation yet):
aws bedrock-agentcore-control create-gateway --name data-agent-gateway --protocol-type MCP \
    --role-arn <GatewayRoleArn> --authorizer-type CUSTOM_JWT \
    --authorizer-configuration '{"customJWTAuthorizer":{"discoveryUrl":"<CognitoDiscoveryUrl>","allowedAudience":["<CognitoAudience>"]}}'
#    then one create-gateway-target per Lambda (targetConfiguration.mcp.lambda + the tool
#    schema in infra/lambda/*.py's docstring, credentialProviderType GATEWAY_IAM_ROLE).

# 7. (optional) The CloudWatch dashboard (dual-identity audit widgets):
./.venv/bin/python scripts/put_dashboard.py     # synthesizes infra/stack.py's dashboard body and PUTs it

# Optional — Lake Formation as the policy authority (edit infra/lakeformation.yaml
# first: governed tables, persona → data access role, each persona's data cells
# filter). Dry-run by default; prints the LF_GOVERNANCE env value for the runtime.
./.venv/bin/python scripts/setup_lakeformation.py           # plan
./.venv/bin/python scripts/setup_lakeformation.py --apply   # converge
# Optional — the Gateway half of the source allowlist (Cedar from persona `sources`)
./.venv/bin/python scripts/setup_gateway_policy.py --mode LOG_ONLY   # then --mode ENFORCE
```

Local one-shot mode needs an explicit principal — the default role is deny-all:

```bash
./.venv/bin/python agent.py --as ops:admin "your question"
./.venv/bin/python agent.py --as exchange-a:junior "your question"   # governed
```

Headless against the deployed runtime: `scripts/jwt_invoke.py` (Cognito login +
bearer, the production path) or `scripts/stream_smoke.py` (SigV4). Both take the
runtime ARN and Cognito values from the same environment variables as the UI.

Persona accounts are an operator action, never self-service (the app client
cannot write `custom:tenant` / `custom:role`; the authorizer validates
`allowedAudience` because Cognito puts custom attributes in the **ID token**):

```bash
aws cognito-idp admin-create-user --user-pool-id <pool> --username finops-a \
    --message-action SUPPRESS \
    --user-attributes Name=custom:tenant,Value=ops Name=custom:role,Value=finops
aws cognito-idp admin-set-user-password --user-pool-id <pool> --username finops-a \
    --password '<strong password>' --permanent
```

All framework knobs are documented in `.env.example`.

## Project structure

| Path | What |
|---|---|
| `agent.py` | Composition root: Strands agent + tools + conversation manager, AgentCore entrypoint, local CLI |
| `data_agent/scheduling/` | L1: AgentCore runtime side (claims, session binding to person + role, SSE envelopes, the feedback entry), the MCP surface (`mcp.py`), the outbound Gateway client (`gateway_client.py`), CLI |
| `semantic/` · `verified_queries/` | The customer-owned assets (L2): metric definitions `semantic/<scenario>.yaml`, table cards `semantic/cards/`, table wiki `semantic/wiki/`, `semantic/glossary.yaml`, verified queries `verified_queries/` — open format, loaded fail-closed, gated by the asset-gate workflow |
| `data_agent/semantic/` | L2: metric model and YAML loader, role-filtered catalog, deterministic compiler, verified queries (`vq.py`: loader, AST literal binding) |
| `data_agent/context/` | L3: data map, wiki + glossary (`wiki.py`), cost gate, result shaping, history compression, prompt assembly |
| `data_agent/governance/` | L4 (authorization only): policies · hardening for every role · sqlglot RLS/CLS rewrite · Lake Formation policy fetch (`lakeformation.py`) · Gateway Cedar rendering (`cedar.py`) |
| `data_agent/compute/` | L5: in-process engine (connection, mounts, lock, schema prober, per-statement bytes / S3 requests), read-only gate |
| `data_agent/storage/` | L6: catalog legs (S3 Tables / Glue / DuckLake), scenarios (`onchain`, `cloudops`) as one `Scenario` object each, table-card loader, registry |
| `data_agent/pipeline.py` · `data_agent/tools.py` | Statement pipeline (gate → semantic → govern → cost → execute stages, one audit line each); the `run_sql` / `search_schema` / `call_metric` / `call_verified` / `list_sources` / `fetch_source` tools |
| `data_agent/audit.py` · `data_agent/feedback.py` | +1: one EMF audit line per statement (with its provenance tier) → CloudWatch metrics; online feedback bucketed by answer source |
| `ui/` | Streamlit chat UI: Cognito login, streamed reasoning, live trace, Trace tab, 👍/👎 with a correction box |
| `infra/` | CDK app: S3 Tables bucket, DuckLake bucket, Cognito pool, least-privilege execution and gateway roles, the CloudWatch dashboard (incl. down-vote rate by answer source); `infra/lakeformation.yaml` — the Lake Formation policy as a declarative file; `infra/lambda/` holds the Gateway source targets |
| `scripts/load_s3tables.py` | On-chain data load — DuckDB writes Iceberg via S3 Tables REST + label tables |
| `scripts/load_ops_data.py` | Cloud-ops data load — CUR 2.0 / CloudTrail / CloudWatch Logs exports to partitioned Parquet |
| `scripts/run_eval.py` + `tests/eval/` | Evaluation suite: yaml cases, trace-event assertions, goldens computed at eval time, bandwidth gate, accuracy by answer source; local or cloud mode |
| `scripts/setup_lakeformation.py` · `scripts/setup_gateway_policy.py` · `scripts/lf_idempotence.py` | Converge Lake Formation to `infra/lakeformation.yaml`; render + attach the Gateway's Cedar policy from persona `sources`; DuckDB-compiled vs Athena-native row-for-row check |
| `scripts/harvest_corrections.py` | Down-votes → a review bundle with draft regression cases (never edits an asset) |
| `.github/workflows/` | `ci` (lint, unit + governance matrix, bandit, semgrep, detect-secrets) and `asset-gate` |
| `scripts/reconcile.py` | Live three-way reconcile: prompt doc ↔ table cards ↔ the real engine schema (exit 1 on drift) |
| `scripts/run_matrix.py` + `tests/eval/gov_matrix.py` | Governance coverage matrix: persona × access path × statement shape, offline, expected outcome derived from the policy |
| `scripts/demo_env.sh` · `scripts/agent_ask.py` | Terminal walkthrough: resolve the deployment into env vars; ask a persona a question and print the pipeline trace beside the answer |
| `scripts/mcp_stdio_proxy.py` · `scripts/claude_code_mcp.sh` | Local stdio MCP server that proxies to the runtime with a self-refreshing token (register once in your coding agent); one-off project-scoped config with a 60-minute token |
| `scripts/mcp_invoke.py` · `scripts/gateway_invoke.py` | The MCP surface as a coding agent sees it; the outbound Gateway and its 401 challenge |
| `scripts/jwt_invoke.py` · `scripts/stream_smoke.py` · `scripts/ab_compare.py` | Headless invoke (JWT / SigV4) · in-process vs query-service comparison |
| `tests/unit/` | 772 unit tests — gate, governance, Lake Formation compile, Cedar rendering, cost, results, working-set handles, context, data map, wiki and card assets, table-card reconcile, governance matrix (196 cells), semantic layer and verified queries (incl. injection forms), feedback, statement stats, eval judges, audit, pipeline stages; no network |
| `docs/design.md` | Design notes: the layers and invariants, identity model, context-layer mechanisms, semantic layer and its assets, Lake Formation as the policy authority, outbound sources, evaluation method, decision table |
| `docs/threat-model/` | Threat model (Threat Composer format + report) |

## Related AWS samples and guidance

This repository is one pattern among several official ones and does not
re-implement them.

- [sample-sagemaker-agentic-analyst](https://github.com/aws-samples/sample-sagemaker-agentic-analyst) — fine-grained access control configured in SageMaker Unified Studio, enforced transparently when an agent queries through Athena and Lake Formation.
- [From silos to insights: federated data access patterns for AI agents](https://aws.amazon.com/blogs/big-data/from-silos-to-insights-federated-data-access-patterns-for-ai-agents/) — catalog-first, direct-source and hybrid access through AgentCore Gateway.
- [Building agentic AI applications with a modern data mesh strategy on AWS](https://aws.amazon.com/blogs/machine-learning/building-agentic-ai-applications-with-a-modern-data-mesh-strategy-on-aws/) — Gateway interceptors, per-hop scoped tokens, Lake Formation row filters.
- [sample-semantic-layer-structured](https://github.com/aws-samples/sample-semantic-layer-structured) — two semantic-layer approaches (virtual knowledge graph vs semantic RAG) with evaluation.
- [sample-okf-llm-wiki](https://github.com/aws-samples/sample-okf-llm-wiki) — a catalog turned into agent-readable knowledge bundles served over MCP.
- [sample-agentic-data-governance](https://github.com/aws-samples/sample-agentic-data-governance) — a four-level context ablation on the BIRD benchmark, measuring what each governance layer is worth.
- [Agent Toolkit for AWS](https://github.com/aws/agent-toolkit-for-aws) — the `aws-data-analytics` plugin for coding agents operating the AWS data stack.

## Cost

Main cost drivers: Amazon Bedrock model invocations (per token), AgentCore
Runtime session compute (two runtimes if you deploy the MCP surface), S3
requests/storage for the sample dataset (a few GB), and S3 Tables
storage/maintenance. The cloud-ops scenario reads exports your account already
produces; its curated Parquet copies add S3 storage only. The optional source
targets add Lambda requests/GB-seconds, AgentCore Gateway requests, and — the
one component that is not storage-only when idle — **Amazon Redshift
Serverless**, billed in RPU-seconds while queries run plus managed storage;
skip it or delete the workgroup when you are not demonstrating the warehouse
path. The dashboard adds CloudWatch custom metrics and Logs Insights scans.
Estimate with the [AWS Pricing Calculator](https://calculator.aws/) for your
region and usage.

## Cleanup

```bash
./scripts/cleanup.sh        # both runtimes, gateway + targets, Lambdas, Redshift, dashboard, data, cdk destroy
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for how to
report security issues. Do not create public GitHub issues for security
findings.

## License

MIT-0. See [LICENSE](LICENSE).
