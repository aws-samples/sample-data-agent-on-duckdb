#!/usr/bin/env bash
# Hook the agent's MCP surface up to Claude Code as a given persona.
#
# What this does: mints that persona's Cognito ID token, then writes a
# project-scoped `.mcp.json` in the target directory with the runtime's MCP URL
# and a static `Authorization: Bearer <token>` header. The runtime's own JWT
# authorizer validates the token; the agent governs for that identity.
#
# Why a static header and not OAuth: this Cognito pool offers no Dynamic Client
# Registration and the runtime authorizer expects an ID token (`aud` = app
# client id, and the custom:tenant / custom:role claims only exist there), so a
# client's automatic authorization-code flow does not produce a usable token.
# The token lives 60 minutes — re-run this script to refresh it.
#
#   source scripts/demo_env.sh
#   scripts/claude_code_mcp.sh finops ~/try-data-agent
#   cd ~/try-data-agent && claude          # /mcp shows data-agent
#
set -euo pipefail
PERSONA="${1:?usage: claude_code_mcp.sh <persona> [dir]   (personas: admin investigator junior finops secops secops_junior)}"
DIR="${2:-$PWD}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$REPO/.venv/bin/python}"
: "${AGENT_MCP_RUNTIME_ARN:?run: source scripts/demo_env.sh}"
: "${EVAL_PASSWORD:?run: source scripts/demo_env.sh}"
: "${COGNITO_CLIENT_SECRET:?run: source scripts/demo_env.sh}"

URL="$("$PY" - <<'PYEOF'
import os, urllib.parse
region = os.environ.get("AGENT_REGION", "us-west-2")
arn = urllib.parse.quote(os.environ["AGENT_MCP_RUNTIME_ARN"], safe="")
print(f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT")
PYEOF
)"
TOKEN="$(cd "$REPO" && "$PY" - "$PERSONA" <<'PYEOF'
import os, sys
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
import run_eval
from jwt_invoke import login
persona = sys.argv[1]
personas = run_eval.load_personas()
if persona not in personas:
    sys.exit(f"unknown persona {persona!r}; known: {', '.join(personas)}")
print(login(personas[persona]["user"], os.environ["EVAL_PASSWORD"], os.environ["COGNITO_CLIENT_SECRET"]))
PYEOF
)"
mkdir -p "$DIR"
umask 077
"$PY" - "$DIR/.mcp.json" "$URL" "$TOKEN" <<'PYEOF'
import json, os, sys
path, url, token = sys.argv[1:4]
config = json.load(open(path)) if os.path.exists(path) else {}
config.setdefault("mcpServers", {})["data-agent"] = {
    "type": "http",
    "url": url,
    "headers": {"Authorization": f"Bearer {token}"},
}
json.dump(config, open(path, "w"), indent=2)
PYEOF
ROLE="$(cd "$REPO" && "$PY" -c "
import sys; sys.path.insert(0, 'scripts')
import run_eval; print(run_eval.load_personas()['$PERSONA']['role'])")"
echo "wrote $DIR/.mcp.json — persona $PERSONA (role $ROLE), token valid 60 min"
echo "next:  cd $DIR && claude        # then /mcp, or just ask a data question"
echo "note:  .mcp.json holds a bearer token — it is chmod 600 and must not be committed"
