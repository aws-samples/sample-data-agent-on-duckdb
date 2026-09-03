"""Streamlit chat UI for the on-chain investigations agent.

A thin client that shows what the in-process engine buys you: it authenticates
to Amazon Cognito, invokes the AgentCore Runtime over HTTPS with the user's JWT
(the production auth path), and renders the streamed SSE envelopes live —
assistant text, the SQL the agent writes, and the per-statement
gate -> rewrite -> execute pipeline running on the in-process DuckDB engine
(with millisecond latencies, no MCP round-trip). A Trace tab reconstructs the
full pipeline for the whole session.

Config comes from env vars (see the sidebar), defaulting to the reference
deployment. Run:  streamlit run ui/app.py
"""

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid

import boto3
import requests
import streamlit as st

# --------------------------------------------------------------------- config
# The runtime + Cognito pool region — NOT the generic AWS_REGION (which may point
# at a Bedrock region for other tooling). All values come from your deployment:
# the CDK stack outputs (Cognito) and `agentcore launch` (runtime ARN).
REGION = os.environ.get("AGENT_REGION", "us-west-2")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")  # required
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")  # required
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "")  # required to log in

# Demo personas for the login dropdown: username → the tenant:role it maps to.
# The row/column policy each principal gets is defined in governance.py.
DEMO_PERSONAS = {
    "inv-a — exchange-a : investigator (sees subject_identity)": "inv-a",
    "jr-a — exchange-a : junior (subject_identity hidden by CLS)": "jr-a",
    "ops-admin — ops : admin (unrestricted)": "ops-admin",
}

# ONE shared investigation script for every persona, so you can log in as each
# and run the identical questions to see where the governance policy changes
# the result. Q4 is the permission litmus test: it explicitly asks for the
# subject_identity (customer PII) column — ops:admin and investigator get it,
# junior is cleanly denied that column (CLS), while all three still see only
# their own tenant's label rows (RLS). Q5 probes the raw-path boundary.
EXAMPLE_QUESTIONS = [
    "The exploiter address is 0x47666fab8bd0ac7003bce3f5c3585383f09486e2. Using "
    "s3t.blockchain.eth_traces, find the single largest inbound internal call to it "
    "(ETH + tx hash).",
    "Build a temp table of that address's outbound internal calls, then report how "
    "many distinct recipients and the equal-amount fan-out pattern.",
    "Join the recipients against cex_deposit_labels — did any funds reach a known "
    "exchange deposit address (cash-out)?",
    "⚖️ Permission test — for every cex_deposit_labels row you can see, list the "
    "address, exchange, risk_tier AND the subject_identity (the customer behind each "
    "deposit address). If any column or row is not available to you, say so explicitly.",
    "⚖️ Permission test — run a direct read_parquet() over the raw eth/traces S3 path "
    "for 2025-02-21 and count the rows.",
]


# ------------------------------------------------------------------ cognito
def _secret_hash(username: str) -> str:
    msg = (username + COGNITO_CLIENT_ID).encode()
    return base64.b64encode(
        hmac.new(COGNITO_CLIENT_SECRET.encode(), msg, hashlib.sha256).digest()
    ).decode()


def login(username: str, password: str) -> dict:
    """Return {id_token, role, tenant} or raise. Uses the ID token because it
    carries the custom:tenant / custom:role claims the agent authorizes on."""
    idp = boto3.client("cognito-idp", region_name=REGION)
    resp = idp.initiate_auth(
        ClientId=COGNITO_CLIENT_ID,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
            "SECRET_HASH": _secret_hash(username),
        },
    )
    if "ChallengeName" in resp:  # e.g. NEW_PASSWORD_REQUIRED for first login
        raise RuntimeError(
            f"Auth challenge {resp['ChallengeName']} — set a permanent password first."
        )
    id_token = resp["AuthenticationResult"]["IdToken"]
    claims = json.loads(base64.urlsafe_b64decode(id_token.split(".")[1] + "=="))
    return {
        "id_token": id_token,
        "tenant": claims.get("custom:tenant", ""),
        "role": claims.get("custom:role", ""),
    }


# --------------------------------------------------------------- SSE invoke
def stream_invoke(id_token: str, prompt: str, session_id: str):
    """Yield parsed SSE envelopes from the runtime as they arrive."""
    escaped = urllib.parse.quote(RUNTIME_ARN, safe="")
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{escaped}/invocations?qualifier=DEFAULT"
    )
    headers = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    with requests.post(
        url, headers=headers, json={"prompt": prompt}, stream=True, timeout=180
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line and line.startswith(b"data: "):
                try:
                    yield json.loads(line[6:])
                except json.JSONDecodeError:
                    continue


# ----------------------------------------------------------------- rendering
# The stream interleaves the model's narration (text deltas) with its tool
# calls (run_sql + the gate→rewrite→execute trace) in true chronological order.
# We render ONE transcript in that order, so a reader follows the full thought
# process and sees each in-process DuckDB call embedded exactly where the agent
# made it — not a wall of prose followed by a separate pile of SQL.
def render_stream(id_token: str, prompt: str, session_id: str) -> dict:
    """Consume the stream, rendering an interleaved transcript live. Returns
    {answer, segments, trace}: `segments` is the ordered transcript (persisted
    so history replay keeps the full reasoning), `trace` backs the Trace tab."""
    container = st.container()
    segments: list[dict] = []  # serializable ordered transcript
    final_trace: list = []
    cur_text: dict | None = None
    cur_tool: dict | None = None

    def _open_text() -> dict:
        with container:
            slot = st.empty()
        seg = {"type": "text", "text": "", "_slot": slot}
        segments.append(seg)
        return seg

    def _open_tool() -> dict:
        with container:
            box = st.container(border=True)
            with box:
                st.markdown(
                    "**⚙️ run_sql** · 🦆 in-process DuckDB — SQL executes in the agent's own process"
                )
                sql_slot = st.empty()
                phase_slot = st.empty()
        seg = {"type": "tool", "sql": "", "phases": [], "_sql": sql_slot, "_phase": phase_slot}
        segments.append(seg)
        return seg

    for env in stream_invoke(id_token, prompt, session_id):
        kind = env.get("kind")
        if kind in ("text", "reasoning"):
            chunk = env.get("text", "")
            if cur_text is None:  # a tool call (or nothing) preceded this — start a fresh block
                cur_text = _open_text()
            cur_text["text"] += chunk
            prefix = (
                "_thinking:_ "
                if kind == "reasoning" and not cur_text["text"].startswith("_")
                else ""
            )
            cur_text["_slot"].markdown(prefix + cur_text["text"])
            cur_tool = None
        elif kind == "tool_start":
            tid = env.get("tool_use_id") or "t"
            if cur_tool is None or cur_tool.get("_tid") != tid:
                cur_tool = _open_tool()
                cur_tool["_tid"] = tid
                cur_text = None  # the tool interrupts the narration; next text is a new block
            cur_tool["sql"] = _pretty_sql(env.get("input") or "")
            cur_tool["_sql"].code(cur_tool["sql"], language="sql")
        elif kind == "trace":
            if cur_tool is not None:
                cur_tool["phases"].append(_phase_line(env))
                cur_tool["_phase"].markdown("\n\n".join(cur_tool["phases"]))
            final_trace.append(env)
        elif kind == "result":
            final_trace = env.get("trace", final_trace)

    # strip live-only slot handles before persisting
    clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in segments]
    answer = "".join(s["text"] for s in clean if s["type"] == "text")
    return {"answer": answer, "segments": clean, "trace": final_trace}


def replay_transcript(segments: list[dict]) -> None:
    """Re-render a stored interleaved transcript (history replay)."""
    for seg in segments:
        if seg["type"] == "text":
            if seg.get("text"):
                st.markdown(seg["text"])
        else:
            with st.container(border=True):
                st.markdown(
                    "**⚙️ run_sql** · 🦆 in-process DuckDB — SQL executes in the agent's own process"
                )
                if seg.get("sql"):
                    st.code(seg["sql"], language="sql")
                if seg.get("phases"):
                    st.markdown("\n\n".join(seg["phases"]))


def _pretty_sql(raw: str) -> str:
    """tool_start input may be a JSON string {"sql": "..."} or a bare string."""
    try:
        obj = json.loads(raw)
        return obj.get("sql", raw) if isinstance(obj, dict) else raw
    except (json.JSONDecodeError, TypeError):
        return raw or "…"


def _phase_line(env: dict) -> str:
    """One markdown line for a gate/rewrite/execute phase in the live pipeline."""
    phase = env.get("phase")
    if phase == "gate":
        verdict = "allowed" if env.get("allowed") else "REJECTED (statement type not permitted)"
        return f"🔒 **gate** — {verdict}"
    if phase == "rewrite":
        if env.get("rejected"):
            return f"🛡️ **govern** — ❌ REJECTED by policy: {env.get('reason', '')}"
        if env.get("governed"):
            bits = []
            for r in env.get("rls") or []:
                bits.append(f"row filter on `{r['table']}` → `{r['predicate']}`")
            for c in env.get("cls") or []:
                bits.append(f"column `{c['column']}` hidden on `{c['table']}`")
            detail = "; ".join(bits) if bits else "policy applied"
            return f"🛡️ **govern** — `{env.get('principal')}`: {detail}"
        return f"🛡️ **govern** — `{env.get('principal')}`: no restriction (unrestricted role)"
    if phase == "execute":
        if "error" in env:
            return "⚡ **execute** — engine error (returned to the model for self-repair)"
        return (
            f"⚡ **execute** — 🦆 in-process DuckDB · **{env.get('engine_ms')} ms** · "
            f"{env.get('rows')} rows · 0 network hops"
        )
    return f"· {phase}"


# ------------------------------------------------------------------- app
def main() -> None:
    st.set_page_config(page_title="On-chain Investigations Agent", page_icon="🔎", layout="wide")
    st.title("🔎 On-chain Investigations Agent")
    st.caption(
        "In-process DuckDB on Amazon Bedrock AgentCore — watch the SQL run in the "
        "engine, inside the agent, with identity-aware row/column governance."
    )

    if "auth" not in st.session_state:
        st.session_state.auth = None
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "traces" not in st.session_state:
        st.session_state.traces = []

    with st.sidebar:
        st.header("Session")
        missing = [
            name
            for name, val in (
                ("AGENT_RUNTIME_ARN", RUNTIME_ARN),
                ("COGNITO_CLIENT_ID", COGNITO_CLIENT_ID),
                ("COGNITO_CLIENT_SECRET", COGNITO_CLIENT_SECRET),
            )
            if not val
        ]
        if missing:
            st.error(
                "Set "
                + ", ".join(missing)
                + " from your deployment (CDK stack outputs + agentcore launch)."
            )
        if st.session_state.auth is None:
            persona_label = st.selectbox("Persona", list(DEMO_PERSONAS))
            username = DEMO_PERSONAS[persona_label]
            password = st.text_input("Password", type="password")
            if st.button("Log in", use_container_width=True) and password:
                try:
                    st.session_state.auth = login(username, password)
                    st.session_state.session_id = uuid.uuid4().hex + uuid.uuid4().hex
                    st.rerun()
                except Exception as e:  # noqa: BLE001 — surface auth errors in UI
                    st.error(f"Login failed: {e}")
        else:
            a = st.session_state.auth
            st.success(f"Principal: **{a['tenant']}:{a['role']}**")
            st.caption(
                "Switch persona = log out and back in as another user — "
                "identity is a validated JWT claim, not a UI toggle."
            )
            if st.button("Log out", use_container_width=True):
                st.session_state.auth = None
                st.session_state.messages = []
                st.session_state.traces = []
                st.rerun()
            st.divider()
            st.subheader("Investigation script")
            st.caption(
                "Same questions for every persona — run them as investigator, then "
                "log out and re-run as junior to see the ⚖️ permission tests diverge."
            )
            for i, q in enumerate(EXAMPLE_QUESTIONS, 1):
                if st.button(f"{i}. {q}", use_container_width=True, key=f"ex{i}"):
                    st.session_state.pending = q

    if st.session_state.auth is None:
        st.info("Log in from the sidebar to start an investigation.")
        return

    chat_tab, trace_tab = st.tabs(["💬 Investigation", "🔬 Trace"])

    with chat_tab:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                if msg["role"] == "assistant" and msg.get("segments") is not None:
                    replay_transcript(msg["segments"])  # full interleaved thought process
                    if msg.get("wall_ms"):
                        st.caption(f"end-to-end {msg['wall_ms']} ms")
                else:
                    st.markdown(msg["content"])

        prompt = st.chat_input("Ask about the case…") or st.session_state.pop("pending", None)
        if prompt:
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                t0 = time.perf_counter()
                out = render_stream(
                    st.session_state.auth["id_token"], prompt, st.session_state.session_id
                )
                wall = round((time.perf_counter() - t0) * 1000)
                st.caption(f"end-to-end {wall} ms")
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": out["answer"],
                    "segments": out["segments"],
                    "wall_ms": wall,
                }
            )
            st.session_state.traces.append({"prompt": prompt, "trace": out["trace"]})

    with trace_tab:
        _render_trace_tab(st.session_state.traces)


def _render_trace_tab(traces: list) -> None:
    if not traces:
        st.info("Run a question to populate the gate → rewrite → execute trace.")
        return
    total_engine = 0.0
    n_stmts = 0
    for item in traces:
        for e in item["trace"]:
            if e.get("phase") == "execute" and isinstance(e.get("engine_ms"), int | float):
                total_engine += e["engine_ms"]
                n_stmts += 1
    c1, c2 = st.columns(2)
    c1.metric("SQL statements executed", n_stmts)
    c2.metric("Total in-process engine time", f"{total_engine:.0f} ms")
    st.caption("Every statement ran in-process — no external query service, no MCP round-trip.")
    for i, item in enumerate(reversed(traces), 1):
        with st.expander(f"Q{len(traces) - i + 1}: {item['prompt'][:80]}…", expanded=(i == 1)):
            for e in item["trace"]:
                st.json(e, expanded=False)


if __name__ == "__main__":
    main()
