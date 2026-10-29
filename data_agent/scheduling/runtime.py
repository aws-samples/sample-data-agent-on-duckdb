"""AgentCore Runtime side: identity claims in, typed SSE envelopes out."""

from __future__ import annotations

import asyncio

from data_agent import audit, governance
from data_agent.context import prompt
from data_agent.scheduling import claims, gateway_client


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
    # The caller's own token is what an outbound gateway call carries.
    # Nothing new enters the process — this is the token we already decode for
    # claims — and it is dropped when the session owner changes.
    gateway_client.set_bearer(auth[7:] if auth.startswith("Bearer ") else None)
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
            # normalize IdP claim names → {tenant, role, sub}: Cognito's
            # custom:tenant / custom:role by default, any other IdP's shape via
            # the CLAIM_*_PATH envs — shared with the MCP surface.
            return claims.normalize(decoded)
        except Exception:  # noqa: BLE001,S110 — fall through to weaker sources  # nosec B110
            pass
    user_id = headers.get("X-Amzn-Bedrock-AgentCore-Runtime-User-Id")
    if user_id and ":" in user_id:  # convention: "<tenant>:<role>"
        tenant, role = user_id.split(":", 1)
        return {"tenant": tenant, "role": role}
    identity = payload.get("identity")
    if isinstance(identity, dict):
        return identity
    return None


# The engine, result store, conversation history and audit counters are all
# module-level and live for the microVM's lifetime. AgentCore gives one
# session per microVM, but the runtimeSessionId is chosen by the CALLER, so
# session isolation is only as good as the caller's id hygiene (documented as
# a shared-responsibility boundary). Nothing in the app re-bound that retained
# state to a principal: a second identity presenting the same session id read
# the first's materialized results and conversation (T003, verified locally —
# a junior counted an admin-materialized _r_1 in full). We bind the session to
# its owner here and reset everything the moment the owner changes.
_session: dict = {"owner": None}


def _owner_key(claims: dict | None, role: str) -> str:
    """Stable identity for the session: the IdP `sub` when present (survives a
    role change for the same user), else the resolved tenant:role."""
    if claims and claims.get("sub"):
        return f"sub:{claims['sub']}"
    return f"role:{role}"


def _bind_session(owner: str) -> bool:
    """Record the session owner; return True when it CHANGED from a previously
    bound owner (→ the caller reused a session id across identities: reset)."""
    from data_agent.context import results

    prev = _session["owner"]
    _session["owner"] = owner
    results.STORE.bind(owner)  # handles minted from now on belong to this owner
    return prev is not None and prev != owner


def _reset_session_state(agent, conversation_manager) -> None:
    """Wipe everything scoped to one principal: the in-process engine (and its
    temp `_r_<n>` results), the result-store bookkeeping, the data map, the L0
    prompt injection, the audit counters and the conversation history."""
    import sys

    from data_agent.compute import engine
    from data_agent.context import datamap, results

    # The rebind audit line is written BEFORE the model runs, so it lands right
    # after the previous invoke's streamed answer text, which Strands' printing
    # handler leaves on stdout without a trailing newline. Terminate that line
    # first, or the EMF JSON is glued to prose and CloudWatch cannot extract its
    # metrics (seen in the cloud: 1 of 37 audit lines).
    sys.stdout.write("\n")
    sys.stdout.flush()
    engine.reset()
    results.STORE.reset()  # handles of the previous owner are gone with the engine
    gateway_client.set_bearer(None)  # never reuse one principal's token for another
    datamap.reset()
    prompt.reset()
    audit.reset_session()
    agent.messages.clear()
    conversation_manager.stubbed = 0
    conversation_manager.reclaimed_bytes = 0


def envelope(kind: str, **fields) -> dict:
    """One typed SSE frame. `kind` drives the UI's rendering switch."""
    return {"kind": kind, **fields}


def new_usage() -> dict:
    """Per-invoke token accounting: summed over every model call of the
    turn from Bedrock's stream `metadata.usage` — no PII, just counts."""
    return {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}


def map_event(ev: dict, trace: list, usage: dict | None = None) -> dict | None:
    """Translate one Strands stream event into a typed UI envelope, and
    accumulate the per-SQL gate→rewrite→execute trace for the final result.

    Strands event shapes (strands-agents 1.54):
      {"data": text, ...}                        -> assistant text delta
      {"reasoningText": text, "reasoning": True} -> chain-of-thought delta
      {"current_tool_use": {toolUseId,name,input}} -> tool call assembling
      {"tool_stream_event": {tool_use, data}}    -> our run_sql phase yields
      {"event": {"metadata": {"usage": {...}}}}  -> one model call's token usage
    """
    meta = ev.get("event", {}).get("metadata") if isinstance(ev.get("event"), dict) else None
    if usage is not None and isinstance(meta, dict) and isinstance(meta.get("usage"), dict):
        u = meta["usage"]
        usage["input_tokens"] += int(u.get("inputTokens") or 0)
        usage["output_tokens"] += int(u.get("outputTokens") or 0)
        usage["model_calls"] += 1
        audit.note_tokens(usage["input_tokens"])  # on the audit line too
        return None
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
       "session": {...counters...}, "usage": {input_tokens, output_tokens, model_calls}}
    The consolidated `trace` array in the final event backs a Trace tab.
    """
    claims = resolve_claims(payload, context)
    role = governance.resolve_role(claims)
    governance.set_active_role(role)
    # When Lake Formation is the policy authority for this persona,
    # fetch its filtered view and install the merged session policy BEFORE
    # anything reads visible_tables (the L0 map, the rewrite). Cached per
    # role; a fetch failure is already a table-level deny inside the policy.
    lf_notes = await asyncio.to_thread(governance.bind_lf_policy, role)
    if lf_notes:
        audit.record(
            principal=role,
            sql="(lake formation policy bind)",
            outcome="rejected" if any("DENIED" in n for n in lf_notes) else "executed",
            stage="lf_policy",
            reason="; ".join(lf_notes)[:400],
        )
    # Bind the session to its owner; if a different identity reused this
    # session id, drop all state retained under the previous principal before
    # doing anything else (T003).
    if _bind_session(_owner_key(claims, role)):
        _reset_session_state(agent, conversation_manager)
        audit.record(
            principal=role,
            sql="(session rebind: principal changed on a reused session id)",
            outcome="rejected",
            stage="session",
            reason="session state reset — a different identity presented this session id",
        )
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
    usage = new_usage()
    yield envelope("start", principal=governance.active_role())
    await asyncio.to_thread(prompt.ensure, agent)  # L0 table map from the engine catalog
    async for ev in agent.stream_async(user_msg):
        if not isinstance(ev, dict):
            continue
        env = map_event(ev, trace, usage)
        if env is not None:
            yield env
    yield envelope(
        "result",
        principal=governance.active_role(),
        trace=trace,
        session=audit.session_counters(compressed=conversation_manager.stubbed),
        usage=usage,  # the eval suite's per-case input-token column
    )
