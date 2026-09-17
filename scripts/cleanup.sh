#!/usr/bin/env bash
# Tear down everything the sample created, in dependency order. Every step is
# idempotent: a resource that is already gone is reported and skipped.
#
#   1. AgentCore runtimes — the HTTP one (`agentcore launch`) and the MCP one
#   2. AgentCore Gateway targets, then the Gateway
#   3. Source Lambdas + their roles (labels API, Redshift source)
#   4. Amazon Redshift Serverless workgroup + namespace (the only idle-cost compute)
#   5. CloudWatch dashboard, SSM persona-password parameter
#   6. Tables inside the S3 Tables bucket (CloudFormation cannot delete a
#      non-empty table bucket)
#   7. The CDK stack (table bucket, DuckLake bucket, roles, log group, dashboard)
#
# Nothing here touches your CUR / CloudTrail source buckets.
set -uo pipefail

REGION="${AWS_REGION:-us-west-2}"
STACK="${STACK_NAME:-DataAgentOnDuckDB}"
GATEWAY_NAME="${GATEWAY_NAME:-data-agent-gateway}"
MCP_RUNTIME_NAME="${MCP_RUNTIME_NAME:-duckdb_analyst_mcp}"
REDSHIFT_NAME="${REDSHIFT_NAME:-data-agent}"
LAMBDAS="${SOURCE_LAMBDAS:-data-agent-labels-api data-agent-redshift-source}"
LAMBDA_ROLES="${SOURCE_LAMBDA_ROLES:-DataAgentLabelsApiRole DataAgentRedshiftSourceRole DataAgentRedshiftRole}"
ACC="aws bedrock-agentcore-control"

echo "== 1/7 AgentCore runtimes =="
if command -v agentcore >/dev/null 2>&1; then
  agentcore destroy 2>/dev/null || echo "  (no HTTP runtime to destroy, or already gone)"
else
  echo "  (agentcore CLI not installed — delete the HTTP runtime in the console if one exists)"
fi
MCP_ID=$($ACC list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?agentRuntimeName=='$MCP_RUNTIME_NAME'].agentRuntimeId | [0]" --output text 2>/dev/null)
if [ -n "$MCP_ID" ] && [ "$MCP_ID" != "None" ]; then
  echo "  deleting MCP runtime $MCP_ID"
  $ACC delete-agent-runtime --region "$REGION" --agent-runtime-id "$MCP_ID" >/dev/null
else
  echo "  (no MCP runtime)"
fi

echo "== 2/7 Gateway targets + gateway =="
GW_ID=$($ACC list-gateways --region "$REGION" \
  --query "items[?name=='$GATEWAY_NAME'].gatewayId | [0]" --output text 2>/dev/null)
if [ -n "$GW_ID" ] && [ "$GW_ID" != "None" ]; then
  for tid in $($ACC list-gateway-targets --region "$REGION" --gateway-identifier "$GW_ID" \
                 --query "items[].targetId" --output text 2>/dev/null); do
    echo "  deleting target $tid"
    $ACC delete-gateway-target --region "$REGION" --gateway-identifier "$GW_ID" --target-id "$tid" >/dev/null
  done
  sleep 5
  echo "  deleting gateway $GW_ID"
  $ACC delete-gateway --region "$REGION" --gateway-identifier "$GW_ID" >/dev/null || \
    echo "  (gateway delete pending target removal — re-run cleanup in a minute)"
else
  echo "  (no gateway)"
fi

echo "== 3/7 Source Lambdas and roles =="
for fn in $LAMBDAS; do
  if aws lambda get-function --region "$REGION" --function-name "$fn" >/dev/null 2>&1; then
    echo "  deleting function $fn"
    aws lambda delete-function --region "$REGION" --function-name "$fn"
  fi
done
for role in $LAMBDA_ROLES DataAgentGatewayRole; do
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    for pol in $(aws iam list-role-policies --role-name "$role" --query "PolicyNames[]" --output text); do
      aws iam delete-role-policy --role-name "$role" --policy-name "$pol"
    done
    echo "  deleting role $role"
    aws iam delete-role --role-name "$role" 2>/dev/null || echo "  (role $role is owned by the CDK stack — cdk destroy removes it)"
  fi
done

echo "== 4/7 Redshift Serverless =="
if aws redshift-serverless get-workgroup --region "$REGION" --workgroup-name "$REDSHIFT_NAME" >/dev/null 2>&1; then
  echo "  deleting workgroup $REDSHIFT_NAME (takes a few minutes)"
  aws redshift-serverless delete-workgroup --region "$REGION" --workgroup-name "$REDSHIFT_NAME" >/dev/null
  until ! aws redshift-serverless get-workgroup --region "$REGION" --workgroup-name "$REDSHIFT_NAME" >/dev/null 2>&1; do
    sleep 15
  done
fi
if aws redshift-serverless get-namespace --region "$REGION" --namespace-name "$REDSHIFT_NAME" >/dev/null 2>&1; then
  echo "  deleting namespace $REDSHIFT_NAME"
  aws redshift-serverless delete-namespace --region "$REGION" --namespace-name "$REDSHIFT_NAME" >/dev/null
fi
SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
  --filters "Name=group-name,Values=data-agent-redshift-noingress" --query "SecurityGroups[0].GroupId" --output text 2>/dev/null)
if [ -n "$SG_ID" ] && [ "$SG_ID" != "None" ]; then
  aws ec2 delete-security-group --region "$REGION" --group-id "$SG_ID" 2>/dev/null || \
    echo "  (security group $SG_ID still attached — re-run after the namespace is gone)"
fi

echo "== 5/7 Dashboard and SSM parameter =="
aws cloudwatch delete-dashboards --region "$REGION" --dashboard-names "$STACK" 2>/dev/null && echo "  dashboard removed" || true
aws ssm delete-parameter --region "$REGION" --name /duckdb-agent/eval/persona-password 2>/dev/null && echo "  persona password parameter removed" || true

echo "== 6/7 S3 Tables contents =="
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

echo "== 7/7 CDK stack =="
if [ -d "$(dirname "$0")/../infra" ]; then
  (cd "$(dirname "$0")/../infra" && cdk destroy --force)
else
  aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
  echo "  delete-stack issued; watch: aws cloudformation describe-stacks --stack-name $STACK"
fi

echo "cleanup complete"
