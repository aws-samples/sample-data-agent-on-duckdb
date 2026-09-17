# Source this before the demo scripts: `source scripts/demo_env.sh`
#
# Everything deployment-specific is an environment variable (no account id or
# pool id lives in code). The values come from the deployment itself:
#   * the runtime ARNs from `agentcore status` / the control-plane API
#   * the Cognito pool and client from the CDK stack outputs
#   * the persona password from SSM Parameter Store (SecureString)
#   * the app client secret from Cognito
# Nothing is echoed: the two secrets are exported, never printed.

export AGENT_REGION="${AGENT_REGION:-us-west-2}"
export AWS_REGION="$AGENT_REGION" AWS_DEFAULT_REGION="$AGENT_REGION"

# --- the two runtimes (HTTP for the UI/eval, MCP for coding agents) ---------
_rt() {
  aws bedrock-agentcore-control list-agent-runtimes --region "$AGENT_REGION" \
    --query "agentRuntimes[?agentRuntimeName=='$1'].agentRuntimeArn | [0]" --output text
}
export AGENT_RUNTIME_ARN="${AGENT_RUNTIME_ARN:-$(_rt duckdb_analyst)}"
export AGENT_MCP_RUNTIME_ARN="${AGENT_MCP_RUNTIME_ARN:-$(_rt duckdb_analyst_mcp)}"

# --- the outbound gateway -----------------------------------
# `list-gateways` does not return the URL, so resolve the id and build it
_gw_id="$(aws bedrock-agentcore-control list-gateways --region "$AGENT_REGION" \
  --query "items[?name=='data-agent-gateway'].gatewayId | [0]" --output text 2>/dev/null)"
if [ -n "${GATEWAY_MCP_URL:-}" ]; then
  :
elif [ -n "$_gw_id" ] && [ "$_gw_id" != "None" ]; then
  export GATEWAY_MCP_URL="$(aws bedrock-agentcore-control get-gateway --region "$AGENT_REGION" \
    --gateway-identifier "$_gw_id" --query gatewayUrl --output text)"
fi
# The source tools this deployment actually exposes (target names = tool-name
# prefixes). The eval skips a `requires_source` case whose source is not here —
# the warehouse target is optional to deploy.
if [ -z "${EVAL_DEPLOYED_SOURCES:-}" ] && [ -n "$_gw_id" ] && [ "$_gw_id" != "None" ]; then
  export EVAL_DEPLOYED_SOURCES="$(aws bedrock-agentcore-control list-gateway-targets \
    --region "$AGENT_REGION" --gateway-identifier "$_gw_id" \
    --query "items[?status=='READY'].name" --output text | tr '\t' ',')"
fi

# --- identity ---------------------------------------------------------------
export COGNITO_POOL_ID="${COGNITO_POOL_ID:?set COGNITO_POOL_ID (CDK output CognitoDiscoveryUrl carries it)}"
export COGNITO_CLIENT_ID="${COGNITO_CLIENT_ID:?set COGNITO_CLIENT_ID (CDK output CognitoAudience)}"
export COGNITO_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --region "$AGENT_REGION" --user-pool-id "$COGNITO_POOL_ID" --client-id "$COGNITO_CLIENT_ID" \
  --query UserPoolClient.ClientSecret --output text)"
export EVAL_PASSWORD="$(aws ssm get-parameter --region "$AGENT_REGION" \
  --name /duckdb-agent/eval/persona-password --with-decryption \
  --query Parameter.Value --output text)"

echo "runtime      : ${AGENT_RUNTIME_ARN##*/}"
echo "mcp runtime  : ${AGENT_MCP_RUNTIME_ARN##*/}"
echo "gateway      : ${GATEWAY_MCP_URL:-<none>} (sources: ${EVAL_DEPLOYED_SOURCES:-<none>})"
echo "identity     : pool ${COGNITO_POOL_ID}, client ${COGNITO_CLIENT_ID}, secret+password loaded"
