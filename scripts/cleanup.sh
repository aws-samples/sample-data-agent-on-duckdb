#!/usr/bin/env bash
# Tear down everything the sample created, in dependency order:
#   1. AgentCore runtime (created by `agentcore launch`)
#   2. Tables inside the S3 Tables bucket (CloudFormation cannot delete a
#      non-empty table bucket)
#   3. The CDK stack (table bucket, DuckLake bucket, role, log group)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK="DataAgentOnDuckDB"

echo "== 1/3 AgentCore runtime =="
if command -v agentcore >/dev/null 2>&1; then
  agentcore destroy 2>/dev/null || echo "  (no runtime to destroy, or already gone)"
else
  echo "  (agentcore CLI not installed — skip; delete the runtime in the console if one exists)"
fi

echo "== 2/3 S3 Tables contents =="
BUCKET_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='TableBucketArn'].OutputValue" --output text 2>/dev/null || true)
if [ -n "$BUCKET_ARN" ] && [ "$BUCKET_ARN" != "None" ]; then
  for ns in $(aws s3tables list-namespaces --table-bucket-arn "$BUCKET_ARN" --region "$REGION" \
                --query "namespaces[].namespace[0]" --output text 2>/dev/null); do
    for tbl in $(aws s3tables list-tables --table-bucket-arn "$BUCKET_ARN" --namespace "$ns" \
                   --region "$REGION" --query "tables[].name" --output text 2>/dev/null); do
      echo "  deleting table $ns.$tbl"
      aws s3tables delete-table --table-bucket-arn "$BUCKET_ARN" --namespace "$ns" \
        --name "$tbl" --region "$REGION"
    done
  done
else
  echo "  (stack or bucket not found — skip)"
fi

echo "== 3/3 CDK stack =="
if [ -d "$(dirname "$0")/../infra" ]; then
  (cd "$(dirname "$0")/../infra" && cdk destroy --force)
else
  aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
  echo "  delete-stack issued; watch: aws cloudformation describe-stacks --stack-name $STACK"
fi

echo "cleanup complete"
