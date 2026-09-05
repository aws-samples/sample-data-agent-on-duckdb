# Data Agent on DuckDB

A data agent whose analytics engine runs **inside the agent process**: in-process
DuckDB queries data directly on Amazon S3 — no cluster, no warehouse, no ETL.
Built on Amazon Bedrock (Claude) and Amazon Bedrock AgentCore Runtime.

What makes it a data agent rather than a SQL tool:

- **Identity-aware authorization** — every statement is rewritten under the
  caller's row/column policy before the engine sees it, from verified JWT
  claims, fail-closed.
- **A context layer between the model and the engine** — a data map generated
  from the engine catalog, a cost gate that refuses unbounded scans, results kept
  whole in the engine instead of truncated, history compressed into pointers.
- **One engine, four ways to reach the same data on S3** — raw Parquet, S3
  Tables, Glue Data Catalog, DuckLake; same agent code, same governance.
- **Every statement visible** — streamed live to the UI as a trace and audited
  as CloudWatch metrics.

The architecture is general-purpose: point it at your own S3 tables and
policies. Data domains are plug-in scenarios; two ship with the repo.

> [!WARNING]
> This is a reference implementation, provided as is. Before production use,
> add monitoring/alerting, rate limiting, HA, fine-grained audit trails, and
> hardened credential rotation for your environment. The exchange/entity
> attribution labels shipped with the on-chain scenario are **synthetic sample
> data**, not real attribution (the OFAC sanctioned-address list is real and
> cited below).

## Five-layer architecture

Each layer has one responsibility and one place to live, listed in the order a
request flows through them. The layering is additive — a semantic layer is the
likely next one.

| Layer | Lives in | Responsibility | Mechanism |
|---|---|---|---|
| **Scheduling** | agent platform (AgentCore Runtime; `data_agent/scheduling/`) | session and resource management | one session = one microVM = one engine; identity claims in, streamed envelopes out; no separate scheduler or workload groups |
| **Context** | agent framework (`data_agent/context/`) | what the model sees and what the engine is asked | data map generated from the engine catalog; partition-filter cost gate; results shaped, never truncated; history compressed into pointers at engine tables |
| **Governance** | SQL-rewrite layer (`data_agent/governance/`) | authorization: who may see what | identity-aware RLS/CLS rewrite before the engine, from verified JWT claims; fail-closed; total by construction |
| **Compute** | in-process DuckDB (`data_agent/compute/`) | SQL execution | the engine runs inside the agent process and reads S3 in place; working sets and oversized results stay in the engine as session temp tables |
| **Storage** | Amazon S3 (`data_agent/storage/`) | durable data | Parquet and native exports, Iceberg tables (S3 Tables, Glue Data Catalog), DuckLake — the same data reachable through four access paths |

<img src="docs/architecture.svg" alt="Architecture: Strands agent, context layer, governance rewrite and embedded DuckDB inside one AgentCore session, four S3 access paths" width="100%">

<details>
<summary>Architecture (text version)</summary>

```
Streamlit UI ──▶ AgentCore Runtime (1 session = 1 microVM = 1 DuckDB)
  (Cognito JWT)   ├─ AuthN: inbound JWT authorizer → verified claims {tenant, role}
   SSE stream ◀── ├─ Strands Agent + Bedrock Claude (writes SQL, reads shaped results)
                  ├─ Context layer    — data map · cost gate · result shaping · history stubs
                  ├─ Governance layer — claims → policy, sqlglot RLS/CLS rewrite + hardening
                  │                     BEFORE the engine
                  └─ DuckDB in-process (httpfs / iceberg / ducklake / fts extensions)
                       ├─ Path A: raw Parquet / exports — S3 path glob → explicit partitions
                       ├─ Path B: S3 Tables     — native Iceberg REST catalog
                       ├─ Path C: Glue catalog  — same table, federated endpoint
                       ├─ Path D: DuckLake      — SQL-database catalog, Parquet on S3
                       └─ _r_<n> result tables · session working sets (temp, die with the session)
Aux: IAM execution role (least privilege) · CloudWatch Logs (one EMF audit line per statement)
Every run_sql streams its gate → govern → cost → execute trace back over the same SSE channel.
```
</details>

## Why in-process

| Mechanism | What a remote query service cannot give you |
|---|---|
| **Millisecond feedback loop** | agents iterate (DESCRIBE → sample → fix → aggregate), 5–10 SQL per question; in-process failures cost milliseconds and zero tokens |
| **Session working set** | intermediate results and oversized results are engine temp tables; "grab a subset, then reason over it" is free — measured 0.5 ms median follow-up |
| **Blast radius** | agent-generated SQL never touches shared production infrastructure; worst case one session OOMs itself; credentials are read-only |
| **No second scheduler** | the agent platform's session scheduling is the resource management — no queue, no workload groups |

**When not to use it**: the data already lives in a governed warehouse (connect
to it), or single queries scan multi-TB working sets. Decision table and A/B
numbers: [docs/design.md](docs/design.md), [docs/ab-results.md](docs/ab-results.md).

## Context: what the model sees, what the engine is asked

The context layer wraps every statement: before the SQL exists (data map),
before it runs (cost gate), after it runs (result shaping) and across turns
(history compression).

<img src="docs/context-flow.svg" alt="Context layer: data map before the SQL exists, cost gate before it runs, result shaping after it runs, history compression across turns" width="100%">

| Mechanism | Before | Now | Knobs |
|---|---|---|---|
| **Data map** | hand-written table list in the prompt | L0 map generated from `duckdb_tables()` at first invoke (attached catalogs included), with each raw source's partition column; `search_schema` ranks tables with DuckDB `fts` BM25, zero network; `DESCRIBE` on demand | `DATAMAP_PROBE_PARTITION` |
| **Cost gate** | "always filter on date" lived in the prompt | a raw partitioned source without a partition predicate is rejected before execution; literal predicates are expanded into explicit per-partition paths (no whole-prefix LIST); composes with the RLS bounds | `MAX_EXPANDED_PARTITIONS` 366 |
| **Result shaping** | rows past a cap were cut off; the model read the head as complete | oversized results are materialized as temp table `_r_<n>`; the model gets row count, columns, three labelled samples and the retrieval path; read-back is ordinary SQL at single-digit ms | `MAX_RESULT_ROWS` 200 · `MAX_RESULT_CHARS` 10000 · `RESULT_TABLE_LRU` 100 |
| **History compression** | framework default: drop old messages by position | at 60 % of the model window (measured from the last Bedrock usage), old materialized results become one line pointing at `_r_<n>`; superseded first, current turn never touched; nothing is lost | `COMPRESSION_THRESHOLD` 0.6 · `MIN_MESSAGES_FOR_STUB` 6 |

Every mechanism relies on the engine being in-process: the data map is read
from the engine catalog, oversized results become engine tables, compressed
history points at those tables. Defaults come from failure modes measured in
production data-agent deployments. Design notes: [docs/design.md](docs/design.md).

## Governance: who may see what

| | Where | Contract |
|---|---|---|
| **AuthN** | AgentCore inbound JWT authorizer + Amazon Cognito | tokens are validated before agent code runs; the ID token's `custom:tenant` / `custom:role` claims are the only identity input |
| **AuthZ** | `data_agent/governance/`, sqlglot rewrite | principal → role → policy; RLS predicates ANDed in, denied columns `EXCLUDE`d, explicit selection of a denied column is a clean rejection naming column and role |
| **Policies** | `GOVERNANCE_POLICIES` (JSON env) | per table `row_filter` / `deny_columns` keyed by `tenant:role`; validated at startup, a typo fails the process |

The rewrite is total by construction — each rule closed a bypass that was proven
executable against this code (rationale in [docs/design.md](docs/design.md)):

| Rule | Closes |
|---|---|
| a restricted role may reference only tables its policy names (plus its own session temp tables and statement-local names) | a mounted-but-unlisted table — another scenario's, or the runtime's own `cw_logs` export — being read ungoverned |
| statement shapes are an allowlist on the parsed root | `EXPLAIN ANALYZE` and other opaque commands passing through ungoverned |
| a local name may not shadow a governed table | `WITH cex_deposit_labels AS (…)` unguarding the real table |
| no policy means the built-in `deny-all` floor | a missing claim becoming the admin role |
| DML behind a CTE prefix is rejected for every role | `WITH x AS (SELECT 1) DELETE …` executing for the unrestricted role |
| host-reaching functions and non-`s3://` paths are refused for every role | `read_text('/proc/self/environ')`, `FROM '/etc/passwd'` |

The session is bound to its principal: a reused `runtimeSessionId` presenting a
different identity resets the engine, materialized results, history and audit
counters before any query runs, so retained state never crosses a principal
change. Governance is total at the **table** level, not only for the tables a
policy happens to name — see [docs/design.md](docs/design.md) and the regenerated
threat model in [`docs/threat-model/`](docs/threat-model/) (20 Mitigated,
7 Accepted, 0 Open).

## Four ways to reach the same data on S3

| Path | Access method | Alias | Resolution | Env toggle |
|---|---|---|---|---|
| **A** | Raw Parquet / native exports | views | S3 path glob, expanded to explicit partitions by the cost gate | always on |
| **B** | S3 Tables (Iceberg REST) | `s3t.*` | S3 Tables native endpoint | `S3_TABLES_ARN` |
| **C** | Glue Data Catalog (Iceberg REST) | `glue.*` | Glue endpoint, federated `s3tablescatalog` — the same table as B | `GLUE_CATALOG` |
| **D** | DuckLake | `dl.*` | SQL catalog database, Parquet on S3 | `DUCKLAKE_CATALOG` |

Same engine, same agent code, same governance; only how a table name resolves
to files differs. All four returned identical results on the incident slice
(3.49M rows).

## Scenarios

A scenario is a data domain plugged into the storage layer: its views over S3,
the partition columns the cost gate must see, a prompt section, and the personas
it ships as default policies. Two are included; further ones (a third domain, a
semantic layer over either) plug in the same way — one module under
`data_agent/storage/scenarios/` and one registry entry.

### Scenario 1 — on-chain fund-flow investigation

**Data.** The AWS Public Blockchain dataset on `s3://aws-public-blockchain`
(Ethereum blocks, transactions, token transfers, traces, logs; hive-partitioned
by day), around a real, publicly reported incident. Two small label tables give
the ledger attribution: the real OFAC sanctioned-address list and a synthetic set
of exchange deposit-address labels. Raw Parquet is one access path; the incident
slice is also loaded into S3 Tables, Glue and DuckLake so all four paths answer
the same questions.

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

## Evaluation and observability

The evaluation suite treats the agent as a black box with a glass pipeline. Cases
are YAML (`tests/eval/cases/*.yaml`): a persona, one or more prompts, and
expectations. The runner logs in to Cognito as a real persona user, calls the
deployed runtime with the JWT exactly as the UI does, and reads the streamed
envelopes. Assertions are made on the **trace events** the pipeline emits for
each statement — which stage rejected and why, which RLS table and CLS column the
rewrite applied, how many partition paths the cost gate expanded, whether a
follow-up read a temp table instead of re-scanning S3 — plus known numbers within
a tolerance and forbidden values absent from the answer. Adversarial cases name
the probe statement: if it executed, the case fails whatever else happened; if
the model refused to issue it, that is defence in depth and is recorded as a
note. Every hop also streams to the UI as a trace event, so the boundary a
request hit is always visible.

| Category | Cases | Pass | What it proves |
|---|---|---|---|
| routing | 5 | 5 | the right access path and table for the question |
| governance-adversarial | 8 | 8 | RLS/CLS applied through every path; bypass shapes stopped |
| cost | 5 | 5 | unbounded raw scans refused; literal filters expanded to explicit paths |
| accuracy | 4 | 4 | incident ground truth reproduced |
| multi-turn | 2 | 2 | working-set reuse and `_r_<n>` read-back across turns |
| ops | 11 | 11 | the second scenario: finops/secops personas, CUR and CloudTrail ground truth |

Full results with per-case traces: [docs/eval-results.md](docs/eval-results.md).

```bash
AGENT_RUNTIME_ARN=<runtime arn> COGNITO_CLIENT_ID=<id> COGNITO_CLIENT_SECRET=<secret> \
EVAL_PASSWORD=<persona password> ./.venv/bin/python scripts/run_eval.py --mode cloud
```

Every `run_sql` also emits one CloudWatch Embedded-Metric-Format line
(`data_agent/audit.py`) — principal, SQL, governance and cost decisions, shape,
rows, engine time — so the runtime log group yields metrics with no metrics API
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

# 4. Chat UI (Cognito login → streamed reasoning + live gate→govern→cost→execute trace)
cd ui && pip install -r requirements.txt
AGENT_RUNTIME_ARN=<runtime arn> COGNITO_CLIENT_ID=<id> COGNITO_CLIENT_SECRET=<secret> \
AGENT_REGION=<your-region> streamlit run app.py
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
| `data_agent/scheduling/` | Scheduling layer: AgentCore runtime side (claims, SSE envelopes, session counters), CLI |
| `data_agent/context/` | Context layer: data map, cost gate, result shaping, history compression, prompt assembly |
| `data_agent/governance/` | Governance layer (authorization only): policies · hardening for every role · sqlglot RLS/CLS rewrite |
| `data_agent/compute/` | Compute layer: in-process engine (connection, mounts, lock, schema prober), read-only gate |
| `data_agent/storage/` | Storage layer: catalog legs (S3 Tables / Glue / DuckLake), scenarios (`onchain`, `cloudops`) as one `Scenario` object each, registry |
| `data_agent/pipeline.py` · `data_agent/tools.py` | Statement pipeline (gate → govern → cost → execute stages, one audit line each); the `run_sql` / `search_schema` tools |
| `data_agent/audit.py` | One EMF audit line per statement → CloudWatch metrics; session counters |
| `ui/` | Streamlit chat UI: Cognito login, streamed reasoning, live trace, Trace tab |
| `infra/` | CDK app: S3 Tables bucket, DuckLake bucket, Cognito pool, least-privilege execution role |
| `scripts/load_s3tables.py` | On-chain data load — DuckDB writes Iceberg via S3 Tables REST + label tables |
| `scripts/load_ops_data.py` | Cloud-ops data load — CUR 2.0 / CloudTrail / CloudWatch Logs exports to partitioned Parquet |
| `scripts/run_eval.py` + `tests/eval/` | Evaluation suite: yaml cases, trace-event assertions, local or cloud mode |
| `scripts/jwt_invoke.py` · `scripts/stream_smoke.py` · `scripts/ab_compare.py` | Headless invoke (JWT / SigV4) · in-process vs query-service comparison |
| `tests/unit/` | 303 unit tests — gate, governance, cost, results, context, data map, audit, ops personas, storage registry, pipeline stages; no network |
| `docs/design.md` | Design notes: five layers, invariants, context-layer mechanisms, governance rationale, scenarios, decision table |
| `docs/threat-model/` | Threat model (Threat Composer format + report) |

## Cost

Main cost drivers: Amazon Bedrock model invocations (per token), AgentCore
Runtime session compute, S3 requests/storage for the sample dataset (a few GB),
and S3 Tables storage/maintenance. The cloud-ops scenario reads exports your
account already produces; its curated Parquet copies add S3 storage only. There
are no always-on compute resources; idle cost is storage only. Estimate with the
[AWS Pricing Calculator](https://calculator.aws/) for your region and usage.

## Cleanup

```bash
./scripts/cleanup.sh        # agentcore runtime + data + cdk destroy
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for how to
report security issues. Do not create public GitHub issues for security
findings.

## License

MIT-0. See [LICENSE](LICENSE).
