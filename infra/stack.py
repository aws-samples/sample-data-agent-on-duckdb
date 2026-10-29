"""The one stack behind the data agent — see app.py for scope rationale."""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_redshiftserverless as rss
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3tables as s3tables
from constructs import Construct

LAMBDA_DIR = Path(__file__).resolve().parent / "lambda"

# The public dataset the solution queries (Registry of Open Data, us-east-2).
PUBLIC_DATASET_ARN = "arn:aws:s3:::aws-public-blockchain"
TABLE_BUCKET_NAME = "data-agent-tables"
NAMESPACE = "blockchain"


class DataAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Path B: S3 Tables bucket + namespace (Iceberg REST, native) ----
        table_bucket = s3tables.CfnTableBucket(
            self,
            "TableBucket",
            table_bucket_name=TABLE_BUCKET_NAME,
            unreferenced_file_removal=s3tables.CfnTableBucket.UnreferencedFileRemovalProperty(
                status="Enabled", noncurrent_days=1, unreferenced_days=1
            ),
        )
        namespace = s3tables.CfnNamespace(
            self,
            "Namespace",
            namespace=NAMESPACE,
            table_bucket_arn=table_bucket.attr_table_bucket_arn,
        )
        namespace.add_dependency(table_bucket)
        # the btc_transactions table itself is created BY DuckDB during data
        # load (scripts/load_s3tables.py) — that write path is part of the solution.

        # --- Path D: DuckLake single-file catalog + Parquet data bucket -----
        ducklake_bucket = s3.Bucket(
            self,
            "DuckLakeBucket",
            bucket_name=f"duckdb-agent-ducklake-{self.account}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # reference implementation: cleanup > durability
        )

        # --- CloudWatch log group for the runtime ---------------------------
        log_group = logs.LogGroup(
            self,
            "RuntimeLogs",
            log_group_name="/aws/bedrock-agentcore/runtimes/duckdb-analyst",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- AgentCore execution role (least privilege, all four paths) -----
        exec_role = iam.Role(
            self,
            "ExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="data-agent-on-duckdb runtime: Bedrock invoke + scoped data access",
        )
        # Bedrock model invocation (foundation models via inference profiles)
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvoke",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                    "arn:aws:bedrock:*::foundation-model/*",
                ],
            )
        )
        # Path A: the public dataset still needs an explicit allow — the
        # runtime signs ALL S3 requests; without this you get 403, not public.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="PublicDatasetRead",
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[PUBLIC_DATASET_ARN, f"{PUBLIC_DATASET_ARN}/*"],
            )
        )
        # Path B: S3 Tables native REST (read; the load script uses your own
        # credentials, not this role, for the one-time write).
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3TablesRead",
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:GetNamespace",
                    "s3tables:ListNamespaces",
                    "s3tables:GetTable",
                    "s3tables:ListTables",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:GetTableData",
                ],
                resources=[
                    table_bucket.attr_table_bucket_arn,
                    f"{table_bucket.attr_table_bucket_arn}/table/*",
                ],
            )
        )
        # Path C: the same table through the Glue federated catalog
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="GlueFederatedRead",
                actions=[
                    "glue:GetCatalog",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                ],
                resources=[
                    f"arn:aws:glue:{self.region}:{self.account}:catalog",
                    f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog",
                    f"arn:aws:glue:{self.region}:{self.account}:catalog/s3tablescatalog/{TABLE_BUCKET_NAME}",
                    f"arn:aws:glue:{self.region}:{self.account}:database/*",
                    f"arn:aws:glue:{self.region}:{self.account}:table/*/*",
                ],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="LakeFormationAccess",
                actions=["lakeformation:GetDataAccess"],
                resources=["*"],  # LF vends per-table credentials; action takes no resource
            )
        )
        # Path D: DuckLake catalog file + Parquet data (and the curated ops/ prefix)
        ducklake_bucket.grant_read(exec_role)
        # --- cloud-ops scenario -------------------------------------------
        # The agent reads the account's own CUR 2.0 export and CloudTrail
        # buckets in place (zero-ETL views) — pass their names at deploy time:
        #   cdk deploy -c opsSourceBuckets=<cur-bucket>,<cloudtrail-bucket>
        ops_buckets = [
            b.strip()
            for b in (self.node.try_get_context("opsSourceBuckets") or "").split(",")
            if b.strip()
        ]
        if ops_buckets:
            # Scope the source-bucket grant to the prefixes the scenario actually
            # reads, NOT the whole bucket. A whole-bucket s3:GetObject let the
            # unrestricted persona read a path outside the pinned OPS account —
            # e.g. read_json over AWSLogs/<other-account>/CloudTrail/ — because
            # the account pin was only a SQL row filter, not an IAM boundary
            # (threat model T007). Pinning object reads to the CUR export
            # prefix and to CloudTrail UNDER the ops account makes it an IAM
            # boundary too. Pass the account and CUR prefix at deploy time:
            #   -c opsSourceBuckets=<cur>,<trail> -c opsAccountId=<id> -c opsCurPrefix=<prefix>
            ops_account = self.node.try_get_context("opsAccountId") or self.account
            cur_prefix = (self.node.try_get_context("opsCurPrefix") or "").strip("/")
            read_prefixes = [f"AWSLogs/{ops_account}/CloudTrail/"]
            if cur_prefix:
                read_prefixes.append(f"{cur_prefix}/")
            object_arns = [f"arn:aws:s3:::{b}/{p}*" for b in ops_buckets for p in read_prefixes]
            exec_role.add_to_policy(
                iam.PolicyStatement(
                    sid="OpsSourceGet",
                    actions=["s3:GetObject"],
                    resources=object_arns,
                )
            )
            # ListBucket (the glob LIST the readers do) restricted to those same
            # prefixes via the s3:prefix condition, so listing cannot enumerate
            # another account's CloudTrail keys either.
            exec_role.add_to_policy(
                iam.PolicyStatement(
                    sid="OpsSourceList",
                    actions=["s3:ListBucket"],
                    resources=[f"arn:aws:s3:::{b}" for b in ops_buckets],
                    conditions={"StringLike": {"s3:prefix": [f"{p}*" for p in read_prefixes]}},
                )
            )
        # CloudWatch Logs export tasks deliver into ops/cwlogs-export/ of the
        # solution bucket; the logs service principal needs these two grants.
        logs_principal = iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")
        # aws:SourceArn pins the confused-deputy condition to THIS log group, not
        # just this account — AWS documents SourceAccount together with SourceArn,
        # and without it any log group in the account could target the export
        # prefix and pollute what the agent presents as its own audit history
        # (threat model T008). The export delivers the runtime's own log group.
        log_group_source = (
            f"arn:aws:logs:{self.region}:{self.account}:"
            "log-group:/aws/bedrock-agentcore/runtimes/duckdb-analyst:*"
        )
        ducklake_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsExportAclCheck",
                principals=[logs_principal],
                actions=["s3:GetBucketAcl"],
                resources=[ducklake_bucket.bucket_arn],
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": log_group_source},
                },
            )
        )
        ducklake_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogsExportWrite",
                principals=[logs_principal],
                actions=["s3:PutObject"],
                resources=[ducklake_bucket.arn_for_objects("ops/cwlogs-export/*")],
                conditions={
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceAccount": self.account,
                    },
                    "ArnLike": {"aws:SourceArn": log_group_source},
                },
            )
        )
        # Logs
        log_group.grant_write(exec_role)

        # --- AuthN: Cognito user pool for the inbound JWT authorizer --------
        # The runtime is configured with a customJWTAuthorizer whose
        # allowedAudience is this app client id; the pool issues ID tokens
        # carrying custom:tenant / custom:role, which the agent maps to a
        # governance policy. Two custom attributes model the tenant:role key.
        user_pool = cognito.UserPool(
            self,
            "AnalystPool",
            user_pool_name="duckdb-agent-analysts",
            self_sign_up_enabled=False,  # operators provision analyst accounts
            custom_attributes={
                "tenant": cognito.StringAttribute(mutable=True),
                "role": cognito.StringAttribute(mutable=True),
            },
            password_policy=cognito.PasswordPolicy(min_length=8),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Attribute permissions decide WHO can set the governance claims. CDK's
        # default app client may read AND write every attribute, custom ones
        # included — so an authenticated junior could call UpdateUserAttributes
        # on their own account, set custom:role=admin, sign in again and become
        # ops:admin. The ID token must still CARRY
        # tenant/role (read), but role assignment is an operator action
        # (AdminUpdateUserAttributes), never self-service: custom:* is absent
        # from the write set.
        readable = (
            cognito.ClientAttributes()
            .with_standard_attributes(email=True, email_verified=True, preferred_username=True)
            .with_custom_attributes("tenant", "role")
        )
        writable = cognito.ClientAttributes().with_standard_attributes(email=True)
        user_pool_client = user_pool.add_client(
            "UiClient",
            user_pool_client_name="duckdb-agent-ui",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=True, admin_user_password=True),
            read_attributes=readable,
            write_attributes=writable,
        )

        # --- The MCP front door -------------------------------
        # The Gateway is the OUTBOUND front door (Redshift MCP server, a
        # labels REST API): inbound it validates the user's JWT, outbound it
        # holds the source credentials so the agent process never does. Its role
        # may invoke exactly one runtime — measured 2026-09-10, an MCP gateway
        # cannot forward the caller's JWT to a runtime target, so the INBOUND
        # MCP path is the runtime's own authorizer, not this hop.
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            role_name="DataAgentGatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
                        )
                    },
                },
            ),
            description="AgentCore Gateway to data-agent targets (least privilege)",
        )
        runtime_arn = self.node.try_get_context("agentRuntimeArn") or ""
        if runtime_arn:
            gateway_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeTheDataAgentRuntimeOnly",
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[runtime_arn, f"{runtime_arn}/*"],
                )
            )
        # --- The two non-lake sources behind the Gateway ----------------
        # Deployed with `-c withSources=true`. The Lambdas are the Gateway's
        # targets (fixed, parameterized, read-only tool surfaces — never free SQL
        # against the warehouse); Redshift Serverless is the warehouse copy of
        # the CUR month, reachable through the Data API only: no public access,
        # a security group with no ingress rule at all, AWS-owned KMS key.
        if self.node.try_get_context("withSources"):
            labels_role = iam.Role(
                self,
                "LabelsApiRole",
                role_name="DataAgentLabelsApiRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    )
                ],
            )
            labels_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadLabelJson",
                    actions=["s3:GetObject"],
                    resources=[ducklake_bucket.arn_for_objects("labels/*.json")],
                )
            )
            labels_fn = lambda_.Function(
                self,
                "LabelsApi",
                function_name="data-agent-labels-api",
                runtime=lambda_.Runtime.PYTHON_3_13,
                handler="labels_api.lambda_handler",
                code=lambda_.Code.from_asset(str(LAMBDA_DIR)),
                role=labels_role,
                timeout=Duration.seconds(20),
                memory_size=256,
                environment={
                    "LABELS_BUCKET": ducklake_bucket.bucket_name,
                    "LABELS_PREFIX_KEY": "labels",
                },
                description="On-chain labels / sanctions REST source for the data agent",
            )

            redshift_role = iam.Role(
                self,
                "RedshiftRole",
                role_name="DataAgentRedshiftRole",
                assumed_by=iam.ServicePrincipal("redshift.amazonaws.com"),
                description="COPY from the solution bucket into Redshift Serverless",
            )
            redshift_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ReadSolutionBucket",
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[
                        ducklake_bucket.bucket_arn,
                        ducklake_bucket.arn_for_objects("ops/*"),
                    ],
                )
            )
            vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)
            no_ingress = ec2.SecurityGroup(
                self,
                "RedshiftNoIngress",
                vpc=vpc,
                security_group_name="data-agent-redshift-noingress",
                description="Redshift Serverless for the data agent: no inbound rules, Data API",
                allow_all_outbound=False,
            )
            namespace = rss.CfnNamespace(
                self,
                "RedshiftNamespace",
                namespace_name="data-agent",
                db_name="dev",
                admin_username="dataagent",
                manage_admin_password=True,  # Secrets Manager holds it; nothing in the template
                iam_roles=[redshift_role.role_arn],
                default_iam_role_arn=redshift_role.role_arn,
                log_exports=["userlog", "connectionlog", "useractivitylog"],
            )
            workgroup = rss.CfnWorkgroup(
                self,
                "RedshiftWorkgroup",
                workgroup_name="data-agent",
                namespace_name=namespace.namespace_name,
                base_capacity=8,
                publicly_accessible=False,
                subnet_ids=[sn.subnet_id for sn in vpc.public_subnets][:3],
                security_group_ids=[no_ingress.security_group_id],
            )
            workgroup.add_dependency(namespace)

            redshift_src_role = iam.Role(
                self,
                "RedshiftSourceRole",
                role_name="DataAgentRedshiftSourceRole",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    )
                ],
            )
            wg_arn = f"arn:aws:redshift-serverless:{self.region}:{self.account}:workgroup/*"
            redshift_src_role.add_to_policy(
                iam.PolicyStatement(
                    sid="DataApiOnThisWorkgroup",
                    actions=[
                        "redshift-data:ExecuteStatement",
                        "redshift-data:DescribeStatement",
                        "redshift-data:GetStatementResult",
                        "redshift-serverless:GetCredentials",
                    ],
                    resources=[wg_arn],
                )
            )
            redshift_src_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ResultsAreCallerScoped",
                    actions=["redshift-data:DescribeStatement", "redshift-data:GetStatementResult"],
                    resources=["*"],
                    conditions={
                        "StringEquals": {
                            "redshift-data:statement-owner-iam-userid": "${aws:userid}"
                        }
                    },
                )
            )
            redshift_fn = lambda_.Function(
                self,
                "RedshiftSource",
                function_name="data-agent-redshift-source",
                runtime=lambda_.Runtime.PYTHON_3_13,
                handler="redshift_source.lambda_handler",
                code=lambda_.Code.from_asset(str(LAMBDA_DIR)),
                role=redshift_src_role,
                timeout=Duration.seconds(60),
                memory_size=256,
                environment={
                    "REDSHIFT_WORKGROUP": "data-agent",
                    "REDSHIFT_DATABASE": "dev",
                    "REDSHIFT_SCHEMA": "ops",
                },
                description="Redshift Serverless warehouse source (read-only, parameterized)",
            )
            # the Gateway may invoke exactly these two functions
            gateway_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeSourceLambdasOnly",
                    actions=["lambda:InvokeFunction"],
                    resources=[labels_fn.function_arn, redshift_fn.function_arn],
                )
            )
            CfnOutput(self, "LabelsApiArn", value=labels_fn.function_arn)
            CfnOutput(self, "RedshiftSourceArn", value=redshift_fn.function_arn)
            CfnOutput(
                self,
                "RedshiftWorkgroupName",
                value=workgroup.workgroup_name,
                description="load with scripts/redshift_load.py (it also grants the source "
                "Lambda's Redshift user USAGE/SELECT on schema ops)",
            )

        # The Gateway resource itself is created out of band (the control-plane
        # API is not a stable CloudFormation resource yet — same reason the
        # runtime is created by `agentcore launch`). Recorded here so the stack
        # owns its role and the operator sees the pairing:
        #   aws bedrock-agentcore-control create-gateway --protocol-type MCP \
        #     --authorizer-type CUSTOM_JWT --role-arn <GatewayRoleArn> \
        #     --authorizer-configuration '{"customJWTAuthorizer": {"discoveryUrl": <pool>, \
        #        "allowedClients": ["<mcp client id>"]}}'
        # NOTE: a Runtime's customJWTAuthorizer must use allowedAudience for
        # Cognito ID tokens (allowedClients matches client_id, which ID tokens
        # do not carry) — verified 2026-09-10.

        # --- One dashboard for the two identity chains ---------------
        # Every statement emits one EMF line (data_agent/audit.py) carrying
        # agent_identity, user_sub (digest), the governance/cost decision and
        # input_tokens. The metrics below come from that line with no PutMetricData
        # call; the log-insights widgets answer "who, on whose behalf, read what".
        dashboard = cloudwatch.Dashboard(
            self,
            "OpsDashboard",
            dashboard_name="DataAgentOnDuckDB",
            default_interval=Duration.days(1),
        )

        def metric(name: str, stat: str = "Sum", **kw) -> cloudwatch.Metric:
            return cloudwatch.Metric(
                namespace="DataAgentOnDuckDB",
                metric_name=name,
                statistic=stat,
                period=Duration.minutes(5),
                **kw,
            )

        outcomes = [
            metric("Statements", dimensions_map={"Outcome": o}, label=o)
            for o in ("executed", "rejected", "error")
        ]
        log_group_name = log_group.log_group_name
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Statements by outcome", left=outcomes, width=12, stacked=True
            ),
            cloudwatch.GraphWidget(
                title="Engine time and rows",
                left=[metric("EngineMs", "Average", label="engine ms (avg)")],
                right=[metric("Rows", label="rows")],
                width=12,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Cost gate: expanded partition paths",
                left=[metric("ExpandedPaths", label="paths")],
                width=8,
            ),
            cloudwatch.GraphWidget(
                title="Materialized results (handles minted)",
                left=[metric("Materialized", label="handles")],
                width=8,
            ),
            cloudwatch.SingleValueWidget(
                title="Rejections (24h)",
                metrics=[metric("Statements", dimensions_map={"Outcome": "rejected"})],
                width=8,
            ),
        )
        dashboard.add_widgets(
            # Evidence chain: what the statements cost at the storage layer
            cloudwatch.GraphWidget(
                title="Bytes scanned and S3 requests",
                left=[metric("BytesScanned", label="bytes scanned")],
                right=[metric("S3Requests", label="S3 requests")],
                width=12,
            ),
            cloudwatch.LogQueryWidget(
                title="Scan cost per session (bytes · S3 requests)",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields audit.user_sub as user",
                    "filter ispresent(audit.bytes_scanned)",
                    "stats sum(audit.bytes_scanned) as bytes_scanned,"
                    " sum(audit.s3_requests) as s3_requests by user",
                    "sort bytes_scanned desc",
                ],
                width=12,
                height=6,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.LogQueryWidget(
                title="Who, on whose behalf, read what (last 50)",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields @timestamp, audit.agent_identity as agent, audit.user_sub as user,"
                    " audit.principal as role, audit.tool as tool, audit.metric as metric,"
                    " Outcome as outcome, audit.stage as refused_at,"
                    " audit.rls.0.table as governed_table, audit.input_tokens as tokens,"
                    " audit.sql",
                    "filter ispresent(audit.principal)",
                    "sort @timestamp desc",
                    "limit 50",
                ],
                width=24,
                height=9,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.LogQueryWidget(
                title="Refusals by stage",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields audit.stage as stage",
                    "filter Outcome != 'executed'",
                    "stats count() as refusals by stage",
                    "sort refusals desc",
                ],
                width=12,
                height=6,
            ),
            cloudwatch.LogQueryWidget(
                title="Input tokens per user (session high-water)",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields audit.user_sub as user, audit.input_tokens as tokens",
                    "filter ispresent(audit.input_tokens) and audit.input_tokens > 0",
                    "stats max(tokens) as session_tokens by user",
                    "sort session_tokens desc",
                ],
                width=12,
                height=6,
            ),
        )

        # The +1 layer's online half: accuracy watched per ANSWER SOURCE.
        # Feedback lines (data_agent/feedback.py) carry the tier of the turn they
        # rate; the down-rate per tier is metric math on two EMF counters, so a
        # decaying bucket shows as a rising line, not an anecdote.
        tiers = ("verified", "metric", "free_sql", "none")
        rate_metrics = {}
        for i, tier in enumerate(tiers):
            rate_metrics[f"n{i}"] = metric("FeedbackNegative", dimensions_map={"Provenance": tier})
            rate_metrics[f"t{i}"] = metric("Feedback", dimensions_map={"Provenance": tier})
        down_rates = [
            cloudwatch.MathExpression(
                expression=f"IF(t{i} > 0, 100 * n{i} / t{i}, 0)",
                using_metrics={f"n{i}": rate_metrics[f"n{i}"], f"t{i}": rate_metrics[f"t{i}"]},
                label=f"{tier} down-rate %",
                period=Duration.hours(1),
            )
            for i, tier in enumerate(tiers)
        ]
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Online accuracy: down-vote rate by answer source (%)",
                left=down_rates,
                width=12,
            ),
            cloudwatch.LogQueryWidget(
                title="Statements and feedback by provenance tier",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields coalesce(audit.provenance, 'n/a') as tier, audit.stage as stage,"
                    " audit.verdict as verdict",
                    "filter ispresent(audit.provenance)",
                    "stats sum(stage != 'feedback' and isblank(verdict)) as statements,"
                    " sum(verdict = 'up') as up, sum(verdict = 'down') as down by tier",
                ],
                width=12,
                height=6,
            ),
        )
        dashboard.add_widgets(
            cloudwatch.LogQueryWidget(
                title="Latest corrections awaiting review (scripts/harvest_corrections.py)",
                log_group_names=[log_group_name],
                query_lines=[
                    "fields @timestamp, audit.provenance as tier, audit.principal as role,"
                    " audit.question as question, audit.correction as correction",
                    "filter audit.stage = 'feedback' and audit.verdict = 'down'",
                    "sort @timestamp desc",
                    "limit 20",
                ],
                width=24,
                height=6,
            ),
        )

        # --- Outputs consumed by agentcore configure / env -----------------
        CfnOutput(
            self,
            "TableBucketArn",
            value=table_bucket.attr_table_bucket_arn,
            description="S3_TABLES_ARN env value (Path B)",
        )
        CfnOutput(
            self,
            "GlueCatalogId",
            value=f"{self.account}:s3tablescatalog/{TABLE_BUCKET_NAME}",
            description="GLUE_CATALOG env value (Path C)",
        )
        CfnOutput(
            self,
            "DuckLakeCatalog",
            value=f"s3://{ducklake_bucket.bucket_name}/catalog/blockchain.ducklake",
            description="DUCKLAKE_CATALOG env value (Path D)",
        )
        CfnOutput(
            self,
            "OpsPrefix",
            value=f"s3://{ducklake_bucket.bucket_name}/ops",
            description="OPS_PREFIX env value (curated CUR / CloudTrail / CloudWatch Logs Parquet)",
        )
        CfnOutput(
            self,
            "LabelsPrefix",
            value=f"s3://{ducklake_bucket.bucket_name}/labels",
            description="LABELS_PREFIX env value (attribution tables)",
        )
        CfnOutput(
            self,
            "ExecutionRoleArn",
            value=exec_role.role_arn,
            description="Pass to: agentcore configure --execution-role",
        )
        CfnOutput(
            self,
            "CognitoDiscoveryUrl",
            value=(
                f"https://cognito-idp.{self.region}.amazonaws.com/"
                f"{user_pool.user_pool_id}/.well-known/openid-configuration"
            ),
            description="customJWTAuthorizer discoveryUrl (for agentcore authorizer config)",
        )
        CfnOutput(
            self,
            "GatewayRoleArn",
            value=gateway_role.role_arn,
            description="create-gateway --role-arn (outbound tool targets)",
        )
        CfnOutput(
            self,
            "DashboardName",
            value=dashboard.dashboard_name,
            description="CloudWatch dashboard with the dual-identity audit widgets",
        )
        CfnOutput(
            self,
            "CognitoAudience",
            value=user_pool_client.user_pool_client_id,
            description="customJWTAuthorizer allowedAudience (app client id)",
        )
