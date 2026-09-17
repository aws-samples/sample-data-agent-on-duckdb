#!/usr/bin/env python3
"""Consume the agent's SSE stream from AgentCore Runtime and print the typed
envelopes as they arrive — the same wire format the chat UI parses.

Usage:
  python scripts/stream_smoke.py "your question" '{"tenant":"exchange-a","role":"investigator"}'

Auth here is SigV4 (boto3). With an inbound JWT authorizer configured, a UI
uses a Bearer token over raw HTTPS instead (see the UI).
"""

import json
import os
import sys
import uuid

import boto3

ARN = os.environ.get("AGENT_RUNTIME_ARN", "")  # required: from `agentcore launch`
REGION = os.environ.get("AGENT_REGION", "us-west-2")


def main() -> None:
    if not ARN:
        sys.exit("set AGENT_RUNTIME_ARN from your deployment (agentcore status prints it)")
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Summarize the incident under investigation."
    identity = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
    payload = {"prompt": prompt}
    if identity:
        payload["identity"] = identity

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=ARN,
        runtimeSessionId=uuid.uuid4().hex + uuid.uuid4().hex,  # >= 33 chars
        payload=json.dumps(payload).encode(),
        qualifier="DEFAULT",
    )
    ctype = resp.get("contentType", "")
    body = resp["response"]
    counts: dict[str, int] = {}
    if "text/event-stream" in ctype:
        for raw in body.iter_lines(chunk_size=10):
            if not raw:
                continue
            line = raw.decode() if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            env = json.loads(line[6:])
            kind = env.get("kind", "?")
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "reasoning":
                sys.stdout.write("\033[2m" + env.get("text", "") + "\033[0m")
            elif kind == "text":
                sys.stdout.write(env.get("text", ""))
            elif kind == "tool_start":
                print(f"\n  ⚙️  {env.get('tool')}({str(env.get('input'))[:80]})")
            elif kind == "trace":
                p = env.get("phase")
                extra = {k: v for k, v in env.items() if k not in ("kind", "phase")}
                print(f"    ├─ {p}: {json.dumps(extra)[:120]}")
            elif kind == "result":
                print(
                    f"\n\n=== RESULT principal={env.get('principal')} "
                    f"trace_phases={len(env.get('trace', []))} ==="
                )
            sys.stdout.flush()
    else:
        data = body.read() if hasattr(body, "read") else b"".join(body)
        print("NON-STREAM contentType=", ctype, "\n", data.decode()[:2000])
    print("\n\nenvelope counts:", counts)


if __name__ == "__main__":
    main()
