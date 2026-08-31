"""The one stack behind the data agent — see app.py for scope rationale."""

from aws_cdk import CfnOutput, RemovalPolicy, Stack
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
                actions=["glue:GetCatalog", "glue:GetDatabase", "glue:GetDatabases",
                         "glue:GetTable", "glue:GetTables"],
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
        # Path D: DuckLake catalog file + Parquet data
        ducklake_bucket.grant_read(exec_role)
        # Logs
        log_group.grant_write(exec_role)

        # --- Outputs consumed by agentcore configure / env -----------------
        CfnOutput(self, "TableBucketArn", value=table_bucket.attr_table_bucket_arn,
                  description="S3_TABLES_ARN env value (Path B)")
        CfnOutput(self, "GlueCatalogId",
                  value=f"{self.account}:s3tablescatalog/{TABLE_BUCKET_NAME}",
                  description="GLUE_CATALOG env value (Path C)")
        CfnOutput(self, "DuckLakeCatalog",
                  value=f"s3://{ducklake_bucket.bucket_name}/catalog/blockchain.ducklake",
                  description="DUCKLAKE_CATALOG env value (Path D)")
        CfnOutput(self, "ExecutionRoleArn", value=exec_role.role_arn,
                  description="Pass to: agentcore configure --execution-role")
