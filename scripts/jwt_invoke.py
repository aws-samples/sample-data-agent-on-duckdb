#!/usr/bin/env python3
"""Log in to Cognito as a persona and invoke the runtime over raw HTTPS with
the Bearer token — the production auth path (inbound JWT authorizer).

With an OAuth/JWT authorizer configured on the runtime, the boto3
`invoke_agent_runtime` SDK cannot be used; the request is a plain HTTPS POST to
the runtime's /invocations endpoint with an Authorization: Bearer header. The
platform validates the JWT BEFORE agent code runs; agent code reads the
verified claims (custom:tenant / custom:role) to pick the governance policy.

Required env: AGENT_RUNTIME_ARN, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET
(from the CDK stack outputs and `agentcore launch`). Optional: AGENT_REGION.

Usage:
  python scripts/jwt_invoke.py <username> <password> "<prompt>"
  python scripts/jwt_invoke.py --no-token "<prompt>"        # expect 401/403
  python scripts/jwt_invoke.py --bad-token "<prompt>"       # expect 401/403
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

import boto3

# All values come from your deployment: CDK stack outputs (Cognito) and
# `agentcore launch` (runtime ARN). No defaults — this script targets YOUR stack.
REGION = os.environ.get("AGENT_REGION", "us-west-2")
CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]
RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]


def _secret_hash(username: str, secret: str) -> str:
    msg = (username + CLIENT_ID).encode()
    return base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()


def login(username: str, password: str, secret: str) -> str:
    idp = boto3.client("cognito-idp", region_name=REGION)
    r = idp.initiate_auth(
        ClientId=CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
            "SECRET_HASH": _secret_hash(username, secret),
        },
    )
    # Use the ID token: Cognito puts custom attributes (custom:tenant /
    # custom:role) in the ID token, not the access token. Its `aud` claim is
    # the app client id, which the runtime's allowedClients authorizer accepts.
    return r["AuthenticationResult"]["IdToken"]


def invoke(token: str | None, prompt: str) -> tuple[int, str]:
    escaped = urllib.parse.quote(RUNTIME_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{escaped}/invocations?qualifier=DEFAULT"
    )
    headers = {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": uuid.uuid4().hex + uuid.uuid4().hex,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps({"prompt": prompt}).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310  # nosec B310 - fixed https AWS endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            body = resp.read().decode()
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def main() -> None:
    secret = os.environ["COGNITO_CLIENT_SECRET"]
    if sys.argv[1] == "--no-token":
        code, body = invoke(None, sys.argv[2])
        print(f"NO-TOKEN → HTTP {code} (expect 401/403)\n{body[:300]}")
        return
    if sys.argv[1] == "--bad-token":
        code, body = invoke("not.a.valid.jwt", sys.argv[2])
        print(f"BAD-TOKEN → HTTP {code} (expect 401/403)\n{body[:300]}")
        return
    username, password, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
    token = login(username, password, secret)
    print(f"logged in as {username}, token len {len(token)}")
    code, body = invoke(token, prompt)
    print(f"HTTP {code}")
    # stream: pull principal + text out of the SSE frames
    principal, text = None, []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                env = json.loads(line[6:])
            except Exception:  # noqa: BLE001, S112
                continue
            if env.get("kind") in ("start", "result") and env.get("principal"):
                principal = env["principal"]
            if env.get("kind") == "text":
                text.append(env.get("text", ""))
    print(f"PRINCIPAL: {principal}")
    print("".join(text)[:1500] if text else body[:1500])


if __name__ == "__main__":
    main()
