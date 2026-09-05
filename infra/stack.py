"""The one stack behind the data agent — see app.py for scope rationale."""

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3tables as s3tables
from constructs import Construct

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
            object_arns = [
                f"arn:aws:s3:::{b}/{p}*" for b in ops_buckets for p in read_prefixes
            ]
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
            "CognitoAudience",
            value=user_pool_client.user_pool_client_id,
            description="customJWTAuthorizer allowedAudience (app client id)",
        )
