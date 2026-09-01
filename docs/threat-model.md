# Comprehensive Threat Model Report

**Generated**: 2026-09-01 17:19:59
**Current Phase**: 1 - Business Context Analysis
**Overall Completion**: 50.0%

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Business Context](#business-context)
3. [Classification Profiles](#classification-profiles)
4. [System Architecture](#system-architecture)
5. [Threat Actors](#threat-actors)
6. [Trust Boundaries](#trust-boundaries)
7. [Assets and Flows](#assets-and-flows)
8. [Threats](#threats)
9. [Mitigations](#mitigations)
10. [Assumptions](#assumptions)
11. [Phase Progress](#phase-progress)

## Executive Summary

Open-source sample (aws-samples, MIT-0) demonstrating a data-analyst agent with an in-process DuckDB engine on Amazon Bedrock AgentCore, including an identity-aware row/column-level governance layer implemented as an engine-neutral SQL rewrite (sqlglot) in front of the embedded engine. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Authentication is delegated to the AgentCore managed inbound JWT authorizer (validates before agent code runs) or SigV4 IAM for calling applications - the sample implements no authentication itself.

### Key Statistics

- **Total Threats**: 11
- **Total Mitigations**: 11
- **Total Assumptions**: 0
- **System Components**: 7
- **Assets**: 0
- **Threat Actors**: 0

## Business Context

**Description**: Open-source sample (aws-samples, MIT-0) demonstrating a data-analyst agent with an in-process DuckDB engine on Amazon Bedrock AgentCore, including an identity-aware row/column-level governance layer implemented as an engine-neutral SQL rewrite (sqlglot) in front of the embedded engine. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Authentication is delegated to the AgentCore managed inbound JWT authorizer (validates before agent code runs) or SigV4 IAM for calling applications - the sample implements no authentication itself.

### Business Features

- **Industry Sector**: Technology
- **Data Sensitivity**: Public
- **System Criticality**: Low
- **Authentication Requirement**: Federated
- **Deployment Model**: Serverless / FaaS

## Classification Profiles

### Software Profile

- **Software Type**: AI / ML System
- **Deployment Model**: Serverless / FaaS
- **Platform / Runtime**: Cloud runtime
- **Description**: Python 3.11-3.13 on Amazon Bedrock AgentCore Runtime (one microVM per session). Three modules: (1) agent.py - Strands Agent + Bedrock Claude with one gated run_sql tool over an in-memory DuckDB (httpfs/iceberg/ducklake extensions), read-only statement gate, resource envelope, identity resolution chain (JWT claims -> runtime user-id header -> payload identity -> default role); (2) governance.py - authorization layer mapping verified principal claims to a role policy, then rewriting every statement with sqlglot: RLS predicates ANDed in via guarded subqueries, denied columns EXCLUDEd (schema-aware via DESCRIBE probe), raw table functions rejected for restricted principals, fail-closed on unparseable SQL; (3) infra/ CDK - S3 buckets (SSE, BlockPublicAccess, enforce_ssl), least-privilege execution role, CloudWatch logs.

### Data Asset Profiles

| ID | Name | Asset | Category | Content Types | Sensitivity | Compliance | States | Volume | Lifecycle | Business Domain | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DP001 | Analytical dataset on S3 | N/A | Structured Data | N/A | Public | N/A | At rest, In transit | N/A | N/A | N/A | Parquet/Iceberg tables on S3 (sample ships against the public aws-public-blockchain dataset; customer-substituted data may be confidential). Per-tenant visibility is enforced by the RLS/CLS rewrite - this is the asset the governance layer protects. |
| DP002 | Identity claims (tenant, role) | N/A | API Data | N/A | Internal | N/A | In transit, In use | N/A | N/A | N/A | Verified JWT claims, or identity asserted by a SigV4-authenticated calling application (runtime user-id header / payload identity). Input to policy resolution; integrity of these claims is the security foundation of the authorization layer. |
| DP003 | Governance policy table | N/A | Configuration Data | N/A | Internal | N/A | At rest, In use | N/A | N/A | N/A | GOVERNANCE_POLICIES JSON env configuration: per-table row_filter / deny_columns + allow_raw_paths switch, keyed by tenant:role. Defines who sees what; owned by the operator. |
| DP004 | DuckLake PostgreSQL credentials | N/A | Secrets and Credentials | N/A | Restricted | N/A | At rest, In use | N/A | N/A | N/A | Optional multi-writer catalog credentials stored in AWS Secrets Manager, fetched at runtime by the execution role; never in env vars or code. |

### User Personas

| ID | Persona | Name | Privilege | Affiliation | Roles | Intent | Entity Type | Authentication | Threat Actor Overlay | In Scope | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| UP001 | Authenticated Standard User | End user (analyst) | Low | N/A | N/A | Legitimate | N/A | Token | N/A | Yes | Asks natural-language questions; JWT validated by the AgentCore inbound authorizer, claims carry tenant/role; may be a restricted principal whose data visibility is limited by RLS/CLS policy. Primary adversary in this model: tries to observe other tenants' rows/columns through crafted questions. |
| UP002 | Non-Human Identity / Service Account | Calling application (SigV4) | Elevated | N/A | N/A | Legitimate | N/A | IAM Role | N/A | Yes | IAM-authenticated infrastructure invoking the runtime; trusted to assert its end-users' identity via the runtime user-id header or payload identity object. Its compromise means arbitrary identity assertion - documented trust assumption. |
| UP003 | Developer / DevOps User | Operator (deployer) | Administrative | N/A | N/A | Legitimate | N/A | IAM Role | N/A | Yes | Customer engineer deploying the sample; owns all env configuration including GOVERNANCE_POLICIES and catalog ARNs. Out of scope as an adversary (owns the account and the data). |
| UP004 | Non-Human Identity / Service Account | LLM (Bedrock Claude) | Low | N/A | N/A | Legitimate | N/A | None | N/A | Yes | Generates SQL from natural language. Untrusted-input channel: its output executes against the engine after the read-only gate and governance rewrite. Subject to prompt injection via user questions; treated as an attacker-controlled SQL source in this model. |

### Non-Functional Requirements

- **Fail-Safe Behaviour**: Revert to safe state — The governance layer must fail closed: a restricted principal must never observe rows or columns outside their policy via any of the four access paths; unparseable SQL, schema-probe failure, or unknown principal must result in rejection or the most restrictive policy - never fail-open.
- **User Error Protection**: Strong — Agent-generated SQL (which may be wrong or adversarial via prompt injection) must not be able to write persistently, exfiltrate to external targets (COPY TO), or attach foreign databases - enforced by the read-only statement gate ahead of governance.

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C001 | AgentCore Runtime (managed) | Compute | AWS | One microVM per session; hosts the agent process. Managed inbound JWT authorizer validates tokens BEFORE agent code runs; header allowlist forwards Authorization. AWS-managed, out of sample scope. |
| C008 | Identity resolution (_resolve_claims) | Security | N/A | agent.py: extracts verified claims in trust order: JWT claims (already validated upstream; decode only) -> X-Amzn-Bedrock-AgentCore-Runtime-User-Id header (SigV4 caller asserts) -> payload identity object -> default role. |
| C009 | Strands Agent + Bedrock Claude | Compute | AWS | LLM writes SQL from natural-language questions; reads tool results. Its SQL output is untrusted input to the pipeline. |
| C010 | run_sql read-only gate (_gate) | Security | N/A | agent.py: statement allowlist - read statements + CREATE TEMP TABLE/VIEW only; rejects persistent writes, ATTACH, COPY TO. First control in the SQL pipeline. |
| C011 | Governance rewrite (governance.py) | Security | N/A | AuthZ layer: principal->role->policy mapping, then sqlglot AST rewrite - RLS predicates via guarded subqueries, CLS via schema-aware EXCLUDE, raw table functions rejected for restricted principals, fail-closed on parse failure. Runs after the gate, before the engine. |
| C012 | DuckDB engine (in-process) | Analytics | N/A | In-memory embedded OLAP engine; dies with the session; READ_ONLY catalog attaches; resource envelope via memory_limit + spill. Executes only gated+governed SQL. |
| C013 | CloudWatch Logs | Other | AWS | Runtime logs incl. per-query engine metrics and governed flag. |

### Connections

| ID | Source | Destination | Protocol | Port | Encrypted | Description |
|---|---|---|---|---|---|---|
| CN001 | AgentCore Runtime (managed) (C001) | Identity resolution (_resolve_claims) (C008) | HTTPS | N/A | Yes | Runtime invokes agent entrypoint with allowlisted headers (Authorization JWT already validated by managed authorizer) + payload. Claims extraction happens in _resolve_claims. |
| CN002 | Identity resolution (_resolve_claims) (C008) | Governance rewrite (governance.py) (C011) | Other | N/A | Yes | Resolved principal (tenant:role) sets the active role for the session; governance resolves role -> policy. |
| CN003 | Strands Agent + Bedrock Claude (C009) | run_sql read-only gate (_gate) (C010) | Other | N/A | Yes | LLM emits SQL via the run_sql tool call; enters the read-only statement gate. UNTRUSTED input crossing. |
| CN004 | run_sql read-only gate (_gate) (C010) | Governance rewrite (governance.py) (C011) | Other | N/A | Yes | Gate-passed SQL enters the governance rewrite (RLS/CLS injection, raw-path rejection, fail-closed parse). |
| CN005 | Governance rewrite (governance.py) (C011) | DuckDB engine (in-process) (C012) | Other | N/A | Yes | Governed SQL executes on the in-process engine under _conn_lock serialization. |
| CN006 | DuckDB engine (in-process) (C012) | S3 analytical data (4 access paths) (D003) | HTTPS | 443 | Yes | Engine reads S3/Iceberg/DuckLake data over httpfs with the execution role's scoped read-only credentials (SigV4, TLS). |
| CN007 | DuckDB engine (in-process) (C012) | Secrets Manager (DuckLake PG credentials) (D004) | HTTPS | 443 | Yes | Optional: fetch DuckLake PostgreSQL catalog credentials from Secrets Manager at attach time. |
| CN008 | Identity resolution (_resolve_claims) (C008) | CloudWatch Logs (C013) | HTTPS | 443 | Yes | Runtime/agent logs to CloudWatch incl. engine metrics and governed flag. |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D003 | S3 analytical data (4 access paths) | Object Storage | Public | Yes | Amazon S3 / S3 Tables / Glue Data Catalog / DuckLake: same logical tables reachable via raw Parquet glob, S3 Tables Iceberg REST, Glue federated Iceberg REST, and DuckLake catalog. All attached READ_ONLY. Buckets: SSE, BlockPublicAccess.BLOCK_ALL, enforce_ssl. Sample data is public; customer-substituted data may be confidential. |
| D004 | Secrets Manager (DuckLake PG credentials) | Other | Restricted | Yes | AWS Secrets Manager: optional PostgreSQL catalog credentials for the multi-writer DuckLake form; fetched at runtime by the execution role; never in env or code. |

## Threat Actors

*No threat actors reviewed for this system.*

## Trust Boundaries

### Trust Zones

#### External (end user + LLM output)

- **Trust Level**: Untrusted
- **Architecture Nodes**: Strands Agent + Bedrock Claude (C009)
- **Description**: Natural-language questions from end users AND the SQL the LLM generates from them. Prompt injection makes LLM output attacker-influenced; both are untrusted.

#### AWS managed platform

- **Trust Level**: High
- **Architecture Nodes**: AgentCore Runtime (managed) (C001), CloudWatch Logs (C013)
- **Description**: AgentCore Runtime (microVM isolation, inbound JWT authorizer), Bedrock service, S3/Glue/Secrets Manager/CloudWatch. AWS-operated; trusted per the shared-responsibility model.

#### Agent process (sample code)

- **Trust Level**: Medium
- **Architecture Nodes**: Identity resolution (_resolve_claims) (C008), run_sql read-only gate (_gate) (C010), Governance rewrite (governance.py) (C011), DuckDB engine (in-process) (C012)
- **Description**: The session's microVM process: identity resolution, read-only gate, governance rewrite, in-process DuckDB. Executes untrusted SQL under two controls; one session = one principal = one engine.

#### Customer account data plane

- **Trust Level**: High
- **Architecture Nodes**: S3 analytical data (4 access paths) (D003), Secrets Manager (DuckLake PG credentials) (D004)
- **Description**: S3 buckets, catalogs, Secrets Manager entries in the customer's own account, reached only via the scoped read-only execution role.

### Trust Boundaries

#### Untrusted SQL -> agent controls

- **Type**: Process
- **Controls**: read-only statement gate (allowlist: read + CREATE TEMP), governance rewrite (RLS via guarded subqueries, schema-aware CLS EXCLUDE), raw table-function rejection for restricted principals, fail-closed on unparseable SQL
- **Description**: LLM-generated SQL (attacker-influenceable via prompt injection) crosses into the agent process. THE central boundary of this model; crossing CN003 -> CN004 -> CN005.

#### Platform -> agent (identity handoff)

- **Type**: Virtual Machine
- **Controls**: AgentCore managed inbound JWT authorizer (signature verification before agent code), header allowlist, trust-ordered fallback chain with default-role floor
- **Description**: Verified identity crosses from the managed authorizer into agent code as claims. AuthN happens platform-side; the sample only consumes verified claims (JWT decode without re-verification is claims EXTRACTION, not authentication). Weaker fallback sources (user-id header / payload identity) are SigV4-caller-asserted - documented trust assumption. Crossing CN001 -> CN002.

#### Agent -> customer data plane

- **Type**: Account
- **Controls**: scoped read-only IAM execution role, READ_ONLY catalog attaches, TLS in transit, SSE + BlockPublicAccess + enforce_ssl on buckets, Secrets Manager for PG credentials
- **Description**: Engine reads S3/catalog data with the scoped read-only execution role over TLS. Only governed SQL output flows back. Crossings CN006, CN007.

## Assets and Flows

*No assets or flows defined for this system.*

## Threats

### Resolved Threats

#### T1: Restricted end user (authenticated, low privilege)

**Statement**: A Restricted end user (authenticated, low privilege) Valid JWT with restricted tenant:role claims; ability to ask arbitrary natural-language questions can Crafts questions that steer the LLM into SQL designed to bypass the RLS/CLS rewrite: raw table functions (read_parquet), catalog-qualified alternate paths (s3t/glue/dl) to the same table, CTE/alias, which leads to Cross-tenant row visibility or denied-column disclosure - defeat of the authorization layer

- **Prerequisites**: Valid JWT with restricted tenant:role claims; ability to ask arbitrary natural-language questions
- **Action**: Crafts questions that steer the LLM into SQL designed to bypass the RLS/CLS rewrite: raw table functions (read_parquet), catalog-qualified alternate paths (s3t/glue/dl) to the same table, CTE/alias
- **Impact**: Cross-tenant row visibility or denied-column disclosure - defeat of the authorization layer
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: prompt-injection, authz-bypass, core-threat
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: AST-level rewrite covers all four access paths and raw-function rejection; gate blocks writes. Residual: novel sqlglot-vs-DuckDB semantic divergence - bounded by fail-closed parsing and read-only worst case.
- **Assessment State**: Current

#### T2: Restricted end user via LLM prompt injection

**Statement**: A Restricted end user via LLM prompt injection Same as above; knowledge that an embedded engine executes generated SQL can Steers the LLM into SQL that is unparseable by sqlglot but valid DuckDB (dialect divergence), hoping the rewrite layer passes it through unmodified, which leads to Ungoverned statement reaches the engine - RLS/CLS bypass

- **Prerequisites**: Same as above; knowledge that an embedded engine executes generated SQL
- **Action**: Steers the LLM into SQL that is unparseable by sqlglot but valid DuckDB (dialect divergence), hoping the rewrite layer passes it through unmodified
- **Impact**: Ungoverned statement reaches the engine - RLS/CLS bypass
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: parser-divergence, fail-closed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Unparseable SQL is rejected for restricted principals (fail-closed). A statement would need to parse cleanly in sqlglot AND mean something different to DuckDB - possible but narrow; worst case still read-only within the session.
- **Assessment State**: Current

#### T3: Malicious or compromised calling application (SigV4 path)

**Statement**: A Malicious or compromised calling application (SigV4 path) IAM permission to invoke the runtime; governance relies on caller-asserted identity when no JWT is present can Asserts an arbitrary tenant:role in the runtime user-id header or payload identity object (e.g. ops:admin) to obtain an unrestricted policy, which leads to Full dataset visibility for any principal the caller invents - authorization is only as strong as the weakest accepted identity source

- **Prerequisites**: IAM permission to invoke the runtime; governance relies on caller-asserted identity when no JWT is present
- **Action**: Asserts an arbitrary tenant:role in the runtime user-id header or payload identity object (e.g. ops:admin) to obtain an unrestricted policy
- **Impact**: Full dataset visibility for any principal the caller invents - authorization is only as strong as the weakest accepted identity source
- **Impacted Assets**: Identity claims (tenant, role)
- **Tags**: identity-assertion, trust-assumption
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Documented trust assumption: the SigV4 caller is IAM-authenticated infrastructure trusted to assert its users' identity (same model as any service-to-service identity propagation). Runtime invoke permission gates who can assert. Documentation hardening (M006) makes this explicit to deployers. For JWT-based deployments the assertion paths are unused.
- **Assessment State**: Current

#### T4: End user with a forged or tampered JWT

**Statement**: A End user with a forged or tampered JWT Runtime deployed WITHOUT the AgentCore inbound JWT authorizer configured (operator misconfiguration), so no platform-side signature verification occurs can Presents a self-signed JWT with arbitrary tenant/role claims; agent code decodes without verification (by design, assuming upstream validation), which leads to Arbitrary principal spoofing -> unrestricted policy

- **Prerequisites**: Runtime deployed WITHOUT the AgentCore inbound JWT authorizer configured (operator misconfiguration), so no platform-side signature verification occurs
- **Action**: Presents a self-signed JWT with arbitrary tenant/role claims; agent code decodes without verification (by design, assuming upstream validation)
- **Impact**: Arbitrary principal spoofing -> unrestricted policy
- **Impacted Assets**: Identity claims (tenant, role)
- **Tags**: deployment-misconfiguration, jwt
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Requires operator misconfiguration (deploying without the authorizer while exposing the endpoint). Sample deploy path configures the authorizer; M006 documentation warning to be added before publication. Inherent to the delegated-AuthN design, which is the correct pattern vs re-implementing JWT verification in agent code.
- **Assessment State**: Current

#### T5: Restricted end user

**Statement**: A Restricted end user Valid restricted session; CLS policy denies a column (e.g. fee) can Infers denied-column values via side channels the rewrite does not remove: error messages, aggregate predicates on permitted expressions, or timing, which leads to Partial disclosure of denied-column information without direct selection

- **Prerequisites**: Valid restricted session; CLS policy denies a column (e.g. fee)
- **Action**: Infers denied-column values via side channels the rewrite does not remove: error messages, aggregate predicates on permitted expressions, or timing
- **Impact**: Partial disclosure of denied-column information without direct selection
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: side-channel, inference
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Inference/side channels against RLS/CLS are an industry-wide limitation of query-rewrite governance (shared with BI-tool row-level security and warehouse RLS). Not-for-production disclaimer + guidance to use physical separation for high-sensitivity multi-tenant data. Sample dataset is public.
- **Assessment State**: Current

#### T6: Restricted end user

**Statement**: A Restricted end user Valid session; engine executes governed but expensive SQL can Asks questions that generate pathological SQL (full-history globs, huge sorts, cross joins) to exhaust the session's memory/CPU, which leads to Own-session OOM/slowdown only - one session = one microVM; no shared-service blast radius. Repeated invocations still incur S3 request and Bedrock token costs

- **Prerequisites**: Valid session; engine executes governed but expensive SQL
- **Action**: Asks questions that generate pathological SQL (full-history globs, huge sorts, cross joins) to exhaust the session's memory/CPU
- **Impact**: Own-session OOM/slowdown only - one session = one microVM; no shared-service blast radius. Repeated invocations still incur S3 request and Bedrock token costs
- **Tags**: resource-exhaustion, cost
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Blast radius is structurally one microVM; envelope caps memory with spill. Cost exposure is the customer's own account and bounded by their throttles/budgets. Remaining: per-request cost, standard for any LLM app.
- **Assessment State**: Current

#### T7: Operator error (customer deployer)

**Statement**: A Operator error (customer deployer) Operator writes GOVERNANCE_POLICIES JSON by hand can Ships a malformed policy (typo in tenant:role key, empty tables map with allow_raw_paths true, wrong row_filter SQL) that silently grants broader visibility than intended, which leads to Unintended data exposure to restricted principals; worst case default role is unrestricted

- **Prerequisites**: Operator writes GOVERNANCE_POLICIES JSON by hand
- **Action**: Ships a malformed policy (typo in tenant:role key, empty tables map with allow_raw_paths true, wrong row_filter SQL) that silently grants broader visibility than intended
- **Impact**: Unintended data exposure to restricted principals; worst case default role is unrestricted
- **Impacted Assets**: Governance policy table
- **Tags**: misconfiguration, policy
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Policy authoring is the operator's responsibility (configuration, same class as IAM policy authoring). Malformed JSON fail-stops today; semantic validation + authoring guidance (M009) planned before publication. governed flag (M010) gives an audit signal.
- **Assessment State**: Current

#### T8: Restricted end user

**Statement**: A Restricted end user Valid restricted session; DESCRIBE-based schema probe active for CLS intersection can Exploits the schema-probe cache: if probe fails open or cache poisons across principals, EXCLUDE lists could shrink, which leads to Denied columns leak through a stale or failed schema probe

- **Prerequisites**: Valid restricted session; DESCRIBE-based schema probe active for CLS intersection
- **Action**: Exploits the schema-probe cache: if probe fails open or cache poisons across principals, EXCLUDE lists could shrink
- **Impact**: Denied columns leak through a stale or failed schema probe
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: schema-probe, cache
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Probe failure keeps the full denylist (fail-closed); cache is per-session/per-principal by construction. Residual only if the customer redeploys the module multi-principal - covered by T011/M006.
- **Assessment State**: Current

#### T9: Any principal via agent-generated SQL

**Statement**: A Any principal via agent-generated SQL Gate must block writes for the model to hold can SQL attempts persistent writes (CREATE TABLE non-temp, INSERT, COPY TO s3://attacker-bucket), ATTACH of foreign databases, or extension installs at query time, which leads to Data exfiltration to external targets or persistence beyond the session

- **Prerequisites**: Gate must block writes for the model to hold
- **Action**: SQL attempts persistent writes (CREATE TABLE non-temp, INSERT, COPY TO s3://attacker-bucket), ATTACH of foreign databases, or extension installs at query time
- **Impact**: Data exfiltration to external targets or persistence beyond the session
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: exfiltration, write-gate
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Three independent layers: statement gate allowlist, READ_ONLY attaches, IAM role without write permissions. COPY TO and ATTACH are explicitly rejected; non-temp CREATE rejected with tests.
- **Assessment State**: Current

#### T10: Concurrent tool calls within one session (Strands thread execution)

**Statement**: A Concurrent tool calls within one session (Strands thread execution) LLM issues multiple run_sql calls in one message; shared DuckDB connection can Race between execute/fetch pairs or between governance schema probe and query execution corrupts result attribution, which leads to Results attributed to the wrong query - integrity issue; in a multi-principal design (NOT this sample: one session = one principal) it would be cross-principal leakage

- **Prerequisites**: LLM issues multiple run_sql calls in one message; shared DuckDB connection
- **Action**: Race between execute/fetch pairs or between governance schema probe and query execution corrupts result attribution
- **Impact**: Results attributed to the wrong query - integrity issue; in a multi-principal design (NOT this sample: one session = one principal) it would be cross-principal leakage
- **Tags**: concurrency, race
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: _conn_lock serializes execute+fetch atomically incl. the schema probe; regression test exists. Single-principal-per-session makes the worst case an integrity glitch, not cross-principal leakage.
- **Assessment State**: Current

### Identified Threats

#### T11: Sample consumer (customer engineer)

**Statement**: A Sample consumer (customer engineer) Customer lifts governance.py into a production multi-principal service can Reuses the module-level active-role slot in a process serving MULTIPLE principals concurrently - a design valid only under AgentCore's one-session-one-microVM model, which leads to Cross-principal policy application: user A's query rewritten under user B's policy

- **Prerequisites**: Customer lifts governance.py into a production multi-principal service
- **Action**: Reuses the module-level active-role slot in a process serving MULTIPLE principals concurrently - a design valid only under AgentCore's one-session-one-microVM model
- **Impact**: Cross-principal policy application: user A's query rewritten under user B's policy
- **Impacted Assets**: Analytical dataset on S3
- **Tags**: misuse-of-sample, documentation
- **Residual Risk Decision**: Open
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Misuse-of-sample risk: module-level active-role slot is only safe under one-session-one-principal. MUST be closed by M006 documentation (module docstring warning + README security considerations) before publication - tracked as the one pre-publication action item.
- **Assessment State**: Current

## Mitigations

### Resolved Mitigations

#### M1: Governance rewrite operates on the parsed AST, not string matching: every reference to a governed logical table - via raw view, s3t.*, glue.* or dl.* alias - is replaced by a guarded subquery (SELECT * EXCLUDE(denied) FROM ref WHERE row_filter); CTE/CTAS-local names are skipped; alias semantics preserved in joins. Raw table functions (read_parquet/read_csv/read_json) are rejected outright for restricted principals.

**Addresses Threats**: T1, T5

#### M2: Fail-closed on parse failure: SQL that sqlglot cannot parse is REJECTED for restricted principals (ValueError), never passed through. Unrestricted principals (allow_raw_paths + no governed tables) bypass rewrite by explicit policy only.

**Addresses Threats**: T1, T2

#### M3: Read-only statement gate ahead of governance: allowlist of read statements + CREATE [OR REPLACE] TEMP[ORARY] TABLE/VIEW on normalized SQL; rejects INSERT/UPDATE/DELETE/non-temp CREATE/ATTACH/COPY TO. DuckDB catalog legs attach READ_ONLY; engine is in-memory and dies with the session; execution role has no write permissions on data buckets - three independent layers against persistence/exfiltration.

**Addresses Threats**: T1, T9

#### M4: Schema-aware CLS fails closed: EXCLUDE lists are intersected with a DESCRIBE-probed schema per reference; if the probe FAILS the full denylist is kept (worst case the engine rejects the query). The probe cache is keyed by reference SQL and lives in one session = one principal = one microVM, so cross-principal poisoning is structurally impossible in the intended deployment.

**Addresses Threats**: T8

#### M5: AuthN is delegated to the AgentCore managed inbound JWT authorizer: signature verification happens platform-side BEFORE agent code runs; the sample's jwt.decode(verify_signature=False) only extracts claims from an already-verified token. README/design doc state this and the identity trust order explicitly.

**Addresses Threats**: T3, T4

#### M7: Session blast-radius isolation: one session = one microVM = one DuckDB; a pathological query OOMs only its own session. Resource envelope (DUCKDB_MEMORY_LIMIT + spill directory) turns memory exhaustion into slower-not-dead. Bedrock/S3 cost exposure bounded by the customer's own account throttles.

**Addresses Threats**: T6

#### M8: run_sql serialization under _conn_lock: execute+fetch pairs are atomic on the shared connection; the governance schema probe takes the same lock. Regression test covers parallel tool calls not stealing each other's results.

**Addresses Threats**: T10

#### M10: Observability of governance decisions: every run_sql result carries a governed: true|false flag and engine metrics; runtime logs to CloudWatch. Gives operators an audit signal for whether the rewrite applied.

**Addresses Threats**: T1, T7

#### M11: Residual acceptance for inference/side channels: aggregate-inference and timing side channels against denied columns are NOT fully closed by any query-rewrite RLS/CLS implementation (industry-wide limitation, same class as BI-tool row-level security). Sample carries a not-for-production disclaimer; production guidance is physical separation (per-tenant tables/views) when denied data is high-sensitivity.

**Addresses Threats**: T5

### Identified Mitigations

#### M6: DOCUMENTATION GAP TO CLOSE: add an explicit deployment warning that (a) without the inbound JWT authorizer configured, JWT claims are NOT verified and the SigV4 identity-assertion paths trust the caller completely; (b) production deployments must treat calling-application identity assertion as a privileged operation (restrict runtime invoke permission to trusted infrastructure only); (c) the module-level active-role slot is valid ONLY under one-session-one-principal (AgentCore microVM) and must not be lifted into multi-principal services.

**Addresses Threats**: T3, T4, T7, T11

#### M9: Policy-as-configuration with fail-safe default: GOVERNANCE_POLICIES is JSON env config; unknown principals resolve to the configured default role. RECOMMENDED HARDENING (documentation): ship the sample's default-role example as a restricted policy and advise operators to validate policy JSON at startup (reject unknown keys / empty-tables-with-allow_raw_paths combinations).

**Addresses Threats**: T7

## Assumptions

*No assumptions defined.*

## Phase Progress

| Phase | Name | Completion |
|---|---|---|
| 1 | Business Context Analysis | 0% 🔄 |
| 2 | Architecture Analysis | 100% ✅ |
| 3 | Threat Actor Analysis | 0% ⏳ |
| 4 | Trust Boundary Analysis | 0% ⏳ |
| 5 | Asset Flow Analysis | 0% ⏳ |
| 6 | Threat Identification | 100% ✅ |
| 7 | Mitigation Planning | 100% ✅ |
| 7.5 | Code Validation Analysis | 0% ⏳ |
| 8 | Residual Risk Analysis | 100% ✅ |
| 9 | Output Generation and Documentation | 100% ✅ |

---

*This threat model report was generated automatically by the Threat Modeling MCP Server.*
