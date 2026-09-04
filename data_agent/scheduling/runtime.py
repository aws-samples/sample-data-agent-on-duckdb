"""AgentCore Runtime side: identity claims in, typed SSE envelopes out."""

from __future__ import annotations

import asyncio

from data_agent import audit, governance
from data_agent.context import prompt


def resolve_claims(payload, context) -> dict | None:
    """Extract identity claims for governance, most-trusted source first.

    1. JWT claims — production path: the inbound JWT authorizer has already
       validated the token before this code runs; with the Authorization
       header allowlisted, decode it here (signature re-check unnecessary).
    2. X-Amzn-Bedrock-AgentCore-Runtime-User-Id header — SigV4 path, set by
       the calling application after ITS OWN authentication of the end user.
    3. payload "identity" object — for callers that pass tenant/role inline
       (the SigV4 caller is IAM-authenticated infrastructure; it is trusted
       to assert its users' identity, same trust model as the header).
    """
    headers = getattr(context, "request_headers", None) or {}
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            import jwt  # PyJWT; validated upstream by the inbound authorizer

            # Deliberate, and the reason is the architecture: AuthN is delegated
            # to AgentCore's managed inbound JWT authorizer, which verifies the
            # signature BEFORE this process is reached (threat model M005). This
            # call is claims EXTRACTION from an already-verified token, not
            # authentication. Re-verifying here would mean shipping JWKS
            # fetching, key rotation and clock handling into the agent for no
            # added assurance. 🔴 If you deploy WITHOUT the inbound authorizer,
            # this line trusts the caller completely — see threats T004/M006.
            claims_only = {"verify_signature": False}  # nosemgrep: unverified-jwt-decode
            decoded = jwt.decode(auth[7:], options=claims_only)  # nosemgrep: unverified-jwt-decode
            # normalize IdP claim names → {tenant, role}. Cognito custom
            # attributes arrive as custom:tenant / custom:role; other IdPs may
            # send bare tenant/role. resolve_role() reads {tenant, role}.
            return {
                "tenant": decoded.get("custom:tenant") or decoded.get("tenant", ""),
                "role": decoded.get("custom:role") or decoded.get("role", ""),
            }
        except Exception:  # noqa: BLE001 — fall through to weaker sources
            pass
    user_id = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-User-Id")
    if user_id and ":" in user_id:  # convention: "<tenant>:<role>"
        tenant, role = user_id.split(":", 1)
        return {"tenant": tenant, "role": role}
    identity = payload.get("identity")
    if isinstance(identity, dict):
        return identity
    return None


def envelope(kind: str, **fields) -> dict:
    """One typed SSE frame. `kind` drives the UI's rendering switch."""
    return {"kind": kind, **fields}


def map_event(ev: dict, trace: list) -> dict | None:
    """Translate one Strands stream event into a typed UI envelope, and
    accumulate the per-SQL gate→rewrite→execute trace for the final result.

    Strands event shapes (strands-agents 1.54):
      {"data": text, ...}                        -> assistant text delta
      {"reasoningText": text, "reasoning": True} -> chain-of-thought delta
      {"current_tool_use": {toolUseId,name,input}} -> tool call assembling
      {"tool_stream_event": {tool_use, data}}    -> our run_sql phase yields
    """
    if "reasoningText" in ev:
        return envelope("reasoning", text=ev["reasoningText"])
    if "data" in ev and isinstance(ev.get("data"), str) and "tool_stream_event" not in ev:
        return envelope("text", text=ev["data"])
    tse = ev.get("tool_stream_event")
    if tse and isinstance(tse.get("data"), dict) and "phase" in tse["data"]:
        phase = tse["data"]
        trace.append(phase)  # consolidated trace for the final result event
        return envelope("trace", **phase)
    ctu = ev.get("current_tool_use")
    if ctu and ctu.get("name"):
        return envelope(
            "tool_start", tool=ctu["name"], tool_use_id=ctu.get("toolUseId"), input=ctu.get("input")
        )
    return None


async def stream(agent, conversation_manager, payload, context=None):
    """The entrypoint body (streaming).

    Yields typed SSE envelopes so a UI can render the run live:
      {"kind":"reasoning"|"text", "text": ...}  — thinking / answer deltas
      {"kind":"tool_start", "tool":"run_sql", "input": ...}
      {"kind":"trace", "phase":"gate"|"rewrite"|"cost"|"execute", ...}  — the
        in-process pipeline, one set per SQL statement
      {"kind":"result", "principal": ..., "trace":[...all phases...],
       "session": {...counters...}}
    The consolidated `trace` array in the final event backs a Trace tab.
    """
    claims = resolve_claims(payload, context)
    governance.set_active_role(governance.resolve_role(claims))
    if governance.is_denied_all():
        # No usable identity → the deny-all floor. Refuse before the model runs:
        # run_sql would reject every statement anyway (defence in depth), but
        # spending Bedrock tokens to discover that is pointless, and the caller
        # deserves one clear answer. Previously this case became ops:admin.
        role = governance.active_role()
        yield envelope("start", principal=role)
        yield envelope(
            "text",
            text="Access denied: no governance policy is assigned to this identity. "
            "Sign in with an account that carries tenant/role claims.",
        )
        yield envelope("result", principal=role, trace=[], denied=True)
        return
    user_msg = payload.get(
        "prompt", "Trace the flow of funds for the incident under investigation."
    )

    trace: list = []
    yield envelope("start", principal=governance.active_role())
    await asyncio.to_thread(prompt.ensure, agent)  # L0 table map from the engine catalog
    async for ev in agent.stream_async(user_msg):
        if not isinstance(ev, dict):
            continue
        env = map_event(ev, trace)
        if env is not None:
            yield env
    yield envelope(
        "result",
        principal=governance.active_role(),
        trace=trace,
        session=audit.session_counters(compressed=conversation_manager.stubbed),
    )
