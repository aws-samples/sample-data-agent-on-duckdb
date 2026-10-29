#!/usr/bin/env python3
"""Evaluation suite runner.

Runs the yaml cases under tests/eval/cases against either the in-process
agent (`--mode local`) or the deployed AgentCore runtime through the real
Cognito → JWT → SSE path (`--mode cloud`), and asserts on the **trace
events** the agent streams (gate / rewrite / cost / execute) plus the final
answer text. Because the trace is structured, the checks are exact — "every
raw scan carried a partition filter", "the junior's rewrite hid this column",
"turn 3 hit the temp table in 4 ms" — no output-text guessing.

Numbers are not copied into the yaml any more:
  * `expect.goldens: [{sql, judge, rel_tol}]` (or the shorthand `golden_sql`
    + `golden_judge`) is RUN at eval time through the statement pipeline as
    `ops:admin` (`--golden-role` to override), on the same engine and the
    same S3 data the agent uses; a tolerant judge (number / set / toplist /
    pair, `scripts/eval_judges.py`) compares the answer text with the rows.
    Golden rows are cached in tests/eval/golden-cache.json (`--golden
    compute|cache|auto`): the worker computes, a laptop without S3 reach
    replays.
  * `pinned_period: YYYY-MM` on a case: if that month is not closed yet the
    goldens are skipped with a note (a moving month has no ground truth).
  * every case records the turn's `input_tokens` / `model_calls` (from the
    runtime's `result` envelope, or the same accounting in local mode).
  * `--no-search-schema` (local mode) runs the agent without the discovery
    tool — the with/without ablation the data-map claims rest on.
  * `requires_env` on a case names runtime env the case needs (the session
    budgets): in local mode it is set for that case and restored after;
    in cloud mode the case is SKIPPED with a note unless the deployed runtime
    already carries those values (a runtime env change is a deploy, not a
    per-case switch).
  * `requires_source` on a case names the Gateway source tool it needs:
    in cloud mode the case is SKIPPED with a note when EVAL_DEPLOYED_SOURCES
    (comma-separated tool-name prefixes the deployment actually exposes) is set
    and does not list it — the warehouse source is optional to deploy and was
    retired from the reference deployment).
    Unset EVAL_DEPLOYED_SOURCES means "everything is deployed": the case runs.

Not a CI gate (it spends Bedrock tokens); run it manually, or by
category on a host with S3 reach:

  EVAL_PASSWORD=... python scripts/run_eval.py --mode cloud
  python scripts/run_eval.py --mode local --only cost-no-date,gov-junior-cls
  python scripts/run_eval.py --mode cloud --category governance-adversarial

Writes docs/eval-results.md (+ .json) — the quantified backing for the
value-prop table.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_judges  # noqa: E402

CASES_DIR = ROOT / "tests" / "eval" / "cases"
PERSONAS_FILE = ROOT / "tests" / "eval" / "personas.yaml"
# --mode cloud targets YOUR deployment: the runtime ARN comes from `agentcore launch`,
# the Cognito values from the CDK stack outputs. No defaults.
GOLDEN_CACHE = ROOT / "tests" / "eval" / "golden-cache.json"
# The locked per-category baseline the bandwidth gate compares against.
# Updated deliberately (--update-baseline, paired with --golden compute after a
# data or view change), never silently — a drifting baseline hides decay.
BASELINE_FILE = ROOT / "tests" / "eval" / "baseline.json"
REGION = os.environ.get("AGENT_REGION", "us-west-2")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")  # required for --mode cloud
GOLDEN_MAX_ROWS = 200  # a golden query is a scalar or a short list, never a dump
_NUM_RE = eval_judges._NUM_RE


# ------------------------------------------------------------------ loading
def load_cases(only: set[str] | None, category: str | None) -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
        if f.name.startswith("."):  # editor / AppleDouble sidecars are not cases
            continue
        for c in yaml.safe_load(f.read_text(encoding="utf-8")) or []:
            c.setdefault("category", f.stem)
            if only and c["id"] not in only:
                continue
            if category and c["category"] != category:
                continue
            cases.append(c)
    return cases


def load_personas() -> dict:
    return yaml.safe_load(PERSONAS_FILE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ runners
class Turn:
    def __init__(self) -> None:
        self.text: list[str] = []
        self.trace: list[dict] = []
        self.principal: str | None = None
        self.http: int | None = None
        self.wall_s: float = 0.0
        self.usage: dict = {}
        self.tools: list[str] = []  # source tools named in fetch_source calls
        self.listed: list[str] = []  # tool names the model called at all

    def feed(self, env: dict) -> None:
        kind = env.get("kind")
        if kind == "tool_start" and env.get("tool"):
            self.listed.append(env["tool"])
        if kind == "tool_start" and env.get("tool") in ("fetch_source", "list_sources"):
            arg = env.get("input")
            if isinstance(arg, str) and '"tool"' in arg:
                import re as _re

                m = _re.search(r'"tool"\s*:\s*"([^"]+)"', arg)
                if m:
                    self.tools.append(m.group(1))
            elif isinstance(arg, dict) and arg.get("tool"):
                self.tools.append(str(arg["tool"]))
        if kind == "text":
            self.text.append(env.get("text", ""))
        elif kind == "trace":
            self.trace.append({k: v for k, v in env.items() if k != "kind"})
        elif kind in ("start", "result") and env.get("principal"):
            self.principal = env["principal"]
        if kind == "result" and isinstance(env.get("usage"), dict):
            self.usage = env["usage"]

    @property
    def answer(self) -> str:
        return "".join(self.text)


_local_agent_cache: dict = {}


def _local_agent(drop: frozenset[str]):
    """The in-process agent; with an ablation (`--no-search-schema`,
    `--no-call-metric`) a twin without those tools (same model, prompt and
    conversation manager)."""
    import agent as agent_mod

    if not drop:
        return agent_mod.agent
    key = ",".join(sorted(drop))
    if key not in _local_agent_cache:
        from strands import Agent

        from data_agent.context import prompt

        tools = [
            t
            for t in (agent_mod.run_sql, agent_mod.search_schema, agent_mod.call_metric)
            if t.tool_name not in drop
        ]
        _local_agent_cache[key] = Agent(
            model=agent_mod.MODEL_ID,
            tools=tools,
            system_prompt=prompt.SYSTEM_PROMPT,
            conversation_manager=agent_mod.conversation_manager,
        )
    return _local_agent_cache[key]


def run_local(prompts: list[str], role: str, drop: frozenset[str] = frozenset()) -> list[Turn]:
    from data_agent import governance
    from data_agent.compute import engine
    from data_agent.context import datamap, prompt, results
    from data_agent.scheduling import runtime

    agent = _local_agent(drop)
    governance.set_active_role(role)
    agent.messages.clear()  # fresh conversation
    results.STORE.reset()
    datamap.reset()
    engine.reset()  # fresh engine (temp tables gone)
    prompt.reset()
    prompt.ensure(agent)  # the L0 table map, exactly as the runtime injects it
    turns: list[Turn] = []

    async def _one(user_prompt: str) -> Turn:
        t = Turn()
        t0 = time.perf_counter()
        raw_trace: list = []
        usage = runtime.new_usage()
        async for ev in agent.stream_async(user_prompt):
            if not isinstance(ev, dict):
                continue
            env = runtime.map_event(ev, raw_trace, usage)
            if env is not None:
                t.feed(env)
        t.principal = governance.active_role()
        t.usage = usage
        t.wall_s = round(time.perf_counter() - t0, 1)
        return t

    for p in prompts:
        turns.append(asyncio.run(_one(p)))
    return turns


def _cloud_token(user: str) -> str:
    from jwt_invoke import login  # scripts/jwt_invoke.py

    secret = os.environ.get("COGNITO_CLIENT_SECRET") or (
        Path(os.environ["COGNITO_CLIENT_SECRET_FILE"]).read_text(encoding="utf-8").strip()
    )
    password = os.environ.get("EVAL_PASSWORD")
    if not password:
        sys.exit("EVAL_PASSWORD (persona password) is required for --mode cloud")
    return login(user, password, secret)


def run_cloud(prompts: list[str], user: str, session_break: bool) -> list[Turn]:
    token = _cloud_token(user)
    escaped = urllib.parse.quote(RUNTIME_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{escaped}/invocations?qualifier=DEFAULT"
    session = uuid.uuid4().hex + uuid.uuid4().hex
    turns: list[Turn] = []
    for prompt in prompts:
        if session_break:
            session = uuid.uuid4().hex + uuid.uuid4().hex
        t = Turn()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session,
        }
        req = urllib.request.Request(
            url, data=json.dumps({"prompt": prompt}).encode(), headers=headers, method="POST"
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310  # nosec B310 - fixed https AWS endpoint  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                t.http = resp.status
                for raw in resp:
                    line = raw.decode() if isinstance(raw, bytes) else raw
                    if line.startswith("data: "):
                        try:
                            t.feed(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            continue
        except urllib.error.HTTPError as e:
            t.http = e.code
            t.text.append(f"[HTTP {e.code}] {e.read().decode()[:300]}")
        t.wall_s = round(time.perf_counter() - t0, 1)
        turns.append(t)
    return turns


# ------------------------------------------------------------------ goldens
def _goldens_of(expect: dict) -> list[dict]:
    """Normalize `goldens: [...]` / `golden_sql` shorthand to a list of specs."""
    out = [dict(g) for g in (expect.get("goldens") or [])]
    if expect.get("golden_sql"):
        out.insert(
            0,
            {
                "sql": expect["golden_sql"],
                "judge": expect.get("golden_judge", "number"),
                "rel_tol": expect.get("rel_tol", 0.01),
            },
        )
    for g in out:
        g.setdefault("judge", "number")
        g.setdefault("rel_tol", 0.01)
    return out


def _golden_key(sql: str, role: str) -> str:
    return hashlib.sha256(f"{role}\n{' '.join(sql.split())}".encode()).hexdigest()[:24]


class GoldenRunner:
    """Runs golden SQL through the statement pipeline (gate → govern → cost →
    execute, one audit line) as the golden role, with a JSON cache."""

    def __init__(self, mode: str, role: str) -> None:
        self.mode = mode  # compute | cache | auto
        self.role = role
        self.cache: dict = {}
        if GOLDEN_CACHE.exists():
            self.cache = json.loads(GOLDEN_CACHE.read_text(encoding="utf-8"))
        self.dirty = False
        self.computed = 0
        self.replayed = 0

    def rows(self, sql: str) -> tuple[list[tuple], str]:
        key = _golden_key(sql, self.role)
        if self.mode in ("cache", "auto") and key in self.cache:
            self.replayed += 1
            return [tuple(r) for r in self.cache[key]["rows"]], "cached"
        if self.mode == "cache":
            raise RuntimeError("golden not in cache and --golden cache forbids computing it")
        rows, engine_ms = self._compute(sql)
        self.cache[key] = {
            "sql": " ".join(sql.split()),
            "role": self.role,
            "rows": rows,
            "engine_ms": engine_ms,
            "computed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        self.dirty = True
        self.computed += 1
        return [tuple(r) for r in rows], f"computed ({engine_ms} ms)"

    def _compute(self, sql: str) -> tuple[list[list], float]:
        from data_agent import governance, pipeline

        prev = governance.active_role()
        governance.set_active_role(self.role)
        try:
            items = list(pipeline.run(sql))
        finally:
            governance.set_active_role(prev)
        text = items[-1]
        if not isinstance(text, str) or text.startswith("ERROR"):
            raise RuntimeError(f"golden SQL failed: {str(text)[:200]}")
        first = text.split("\n", 1)[0]
        head = json.loads(first)
        if isinstance(head, dict) and head.get("shape") == "summary":
            raise RuntimeError(
                f"golden SQL returned {head.get('rows')} rows — a golden must fit inline "
                f"(≤ {GOLDEN_MAX_ROWS}); add a LIMIT or aggregate"
            )
        engine_ms = next(
            (e.get("engine_ms", 0.0) for e in items if isinstance(e, dict) and "engine_ms" in e),
            0.0,
        )
        # golden rows go to the judges as VALUES, without the data:«…» marking the
        # model-facing string carries — a judge compares against the answer
        # text the model wrote, which quotes the value, not the wrapper
        from data_agent.context import results as results_mod

        return [[results_mod.unmark(v) for v in r.values()] for r in head], engine_ms

    def save(self) -> None:
        if self.dirty:
            GOLDEN_CACHE.write_text(
                json.dumps(self.cache, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )


def _reload_env_knobs() -> None:
    """Re-read the env-driven knobs a case's `requires_env` changed (local mode:
    the modules were imported before the case set them)."""
    from data_agent.context import cost as cost_mod

    cost_mod.SESSION_MAX_STATEMENTS = int(os.environ.get("SESSION_MAX_STATEMENTS", "0"))
    cost_mod.SESSION_MAX_EXPANDED_PATHS = int(os.environ.get("SESSION_MAX_EXPANDED_PATHS", "0"))
    cost_mod.SESSION_MAX_ENGINE_MS = float(os.environ.get("SESSION_MAX_ENGINE_MS", "0"))
    cost_mod.UPGRADE_ADVICE_PATHS = int(os.environ.get("UPGRADE_ADVICE_PATHS", "120"))


def period_is_closed(pinned: str, today: dt.date | None = None) -> bool:
    """A `pinned_period` YYYY-MM is closed once the current UTC month is later."""
    today = today or dt.datetime.now(dt.UTC).date()
    y, m = (int(x) for x in pinned.split("-")[:2])
    return (today.year, today.month) > (y, m)


# ------------------------------------------------------------- assertions
def _phases(turns: list[Turn], phase: str, from_turn: int = 0) -> list[dict]:
    return [e for t in turns[from_turn:] for e in t.trace if e.get("phase") == phase]


def _sqls(turns: list[Turn], from_turn: int = 0) -> list[str]:
    return [e.get("sql", "") for e in _phases(turns, "gate", from_turn)]


def _executed_sqls(turns: list[Turn]) -> list[str]:
    """SQL that reached the engine and ran without error (gate sql → execute)."""
    out = []
    for t in turns:
        current = ""
        for e in t.trace:
            if e.get("phase") == "gate":
                current = e.get("sql", "")
            elif e.get("phase") == "execute" and "error" not in e:
                out.append(current)
    return out


def _tool_calls(turns: list[Turn]) -> list[str]:
    """Names of the non-SQL tools the model called, from the tool_start envelopes
    (a `fetch_source` call is not a pipeline statement, so it has no trace)."""
    return [t for turn in turns for t in getattr(turn, "tools", [])]


def _rejections(turns: list[Turn]) -> list[str]:
    out = []
    for t in turns:
        for e in t.trace:
            if e.get("phase") == "gate" and e.get("allowed") is False:
                out.append("gate: statement type not permitted")
            elif e.get("rejected"):
                out.append(f"{e.get('phase')}: {e.get('reason', '')}")
    return out


def _numbers(text: str) -> list[float]:
    return eval_judges.numbers(text)


def evaluate(
    expect: dict, turns: list[Turn], goldens: GoldenRunner | None = None, pinned: str | None = None
) -> tuple[bool, list[str]]:
    notes: list[str] = []
    ok = True
    answer = "\n".join(t.answer for t in turns)
    rej = _rejections(turns)

    def fail(msg: str) -> None:
        nonlocal ok
        ok = False
        notes.append("FAIL " + msg)

    if expect.get("http_ok", True) and any(t.http not in (None, 200) for t in turns):
        fail(f"http {[t.http for t in turns]}")
    # Adversarial cases: the model may refuse a destructive/exfiltrating statement
    # before the tool is ever called. That is defence in depth working, not a gate
    # failure — the gate mechanics are unit-tested; here either layer stopping the
    # statement passes (with a note), as long as nothing leaked.
    # `probe_sql_regex` names the statement under test. If it EXECUTED, that is
    # the bypass the case exists to catch — fail regardless of anything else.
    probe = expect.get("probe_sql_regex")
    if probe and any(re.search(probe, q, re.I | re.S) for q in _executed_sqls(turns)):
        fail(f"probe statement /{probe}/ executed — the rule did not stop it")
    refused = expect.get("accept_model_refusal") and not _sqls(turns) and not rej
    # ...or the model complied with the spirit but not the letter: it ran other,
    # harmless statements and never issued the probe. Same verdict as a refusal.
    declined = (
        expect.get("accept_model_refusal")
        and probe
        and not refused
        and not any(re.search(probe, q, re.I | re.S) for q in _sqls(turns))
    )
    if refused:
        notes.append("note: model refused before the tool was called (gate not exercised)")
    elif declined:
        notes.append("note: model declined to issue the probe statement (gate not exercised)")
    if refused or declined:
        expect = {
            k: v
            for k, v in expect.items()
            if k not in ("any_rejected", "rejected_contains", "sql_regex_any")
        }
    if expect.get("any_rejected") and not rej:
        fail("expected a rejection, none happened")
    if expect.get("no_rejections") and rej:
        fail(f"unexpected rejections: {rej[:2]}")
    if s := expect.get("rejected_contains"):
        if not any(s.lower() in r.lower() for r in rej):
            fail(f"no rejection containing {s!r} (got {rej[:2]})")
    if pat := expect.get("sql_regex_any"):
        if not any(re.search(pat, q, re.I | re.S) for q in _sqls(turns)):
            fail(f"no executed SQL matches /{pat}/")
    if pat := expect.get("sql_regex_none"):
        if any(re.search(pat, q, re.I | re.S) for q in _sqls(turns)):
            fail(f"an executed SQL matches forbidden /{pat}/")
    if expect.get("governed_any") and not any(e.get("governed") for e in _phases(turns, "rewrite")):
        fail("no governed rewrite")
    # ---- semantic layer
    if m := expect.get("metric_called"):
        if not any(
            e.get("metric") == m and not e.get("rejected") for e in _phases(turns, "semantic")
        ):
            fail(f"metric {m!r} was not compiled through call_metric")
    if expect.get("semantic_rejected") and not any(
        e.get("rejected") for e in _phases(turns, "semantic")
    ):
        fail("expected the semantic layer to refuse the call, it did not")
    # ---- outbound sources: the audit line is the evidence, not the trace
    if want := expect.get("source_fetched"):
        got = [t for t in _tool_calls(turns) if t == want]
        if not got:
            fail(f"source tool {want!r} was not fetched (tools called: {_tool_calls(turns)})")
    if want := expect.get("source_attempted"):
        if want not in _tool_calls(turns):
            fail(f"the model never attempted source tool {want!r} (tried: {_tool_calls(turns)})")
    if expect.get("sources_listed") and not any(
        "list_sources" in getattr(t, "listed", []) for t in turns
    ):
        fail("the model never listed the available sources")
    # ---- session budget
    if expect.get("budget_refused") and not any(e.get("budget") for e in _phases(turns, "cost")):
        fail("expected a session-budget refusal, none happened")
    # ---- upgrade advice: past the envelope → a route, not an execution
    if expect.get("upgrade_advised") and not any(e.get("upgrade") for e in _phases(turns, "cost")):
        fail("expected an upgrade suggestion, none happened")
    if tbl := expect.get("rls_table"):
        if not any(
            r.get("table") == tbl for e in _phases(turns, "rewrite") for r in (e.get("rls") or [])
        ):
            fail(f"no RLS applied on {tbl}")
    if col := expect.get("cls_hidden"):
        hidden = any(
            c.get("column") == col for e in _phases(turns, "rewrite") for c in (e.get("cls") or [])
        )
        denied = any(col in r for r in rej)
        if not (hidden or denied):
            fail(f"column {col} neither hidden nor denied")
    if n := expect.get("expanded_paths_min"):
        got = sum(e.get("expanded_paths") or 0 for e in _phases(turns, "cost"))
        if got < n:
            fail(f"expanded paths {got} < {n}")
    if expect.get("partition_filter_all"):
        bad = [
            e
            for e in _phases(turns, "cost")
            if not e.get("rejected") and not e.get("partition_filter")
        ]
        if bad:
            fail("a raw scan ran without a partition filter")
    if n := expect.get("materialized_min"):
        got = sum(1 for e in _phases(turns, "execute") if e.get("shape") == "summary")
        if got < n:
            fail(f"materialized results {got} < {n}")
    if expect.get("readback") and not any(re.search(r"\b_r_\d+\b", q) for q in _sqls(turns)):
        fail("no read-back query against a materialized _r_ table")
    if expect.get("no_raw_path_executed"):
        # every statement that names a raw file path must have been stopped before
        # the engine — whether the model tried it or (prompt rule) avoided it
        raw = re.compile(r"read_parquet|read_csv|read_json|parquet_scan|s3://|'/", re.I)
        for t in turns:
            current = ""
            for e in t.trace:
                if e.get("phase") == "gate":
                    current = e.get("sql", "")
                elif e.get("phase") == "execute" and "error" not in e and raw.search(current):
                    fail(f"a raw-path statement executed: {current[:80]!r}")
    for s in expect.get("answer_contains", []) or []:
        if s.lower() not in answer.lower():
            fail(f"answer lacks {s!r}")
    for s in expect.get("answer_not_contains", []) or []:
        if s.lower() in answer.lower():
            fail(f"answer contains forbidden {s!r}")
    if spec := expect.get("answer_number"):
        # legacy literal (kept for cases whose truth is a fixed constant); new
        # cases use goldens computed at eval time
        target, tol = float(spec["value"]), float(spec.get("rel_tol", 0.01))
        nums = _numbers(answer)
        if not any(abs(v - target) <= tol * abs(target) for v in nums):
            fail(f"no number within {tol:.0%} of {target} in answer (saw {nums[:8]})")
    # ---- runtime goldens
    specs = _goldens_of(expect)
    if specs and pinned and not period_is_closed(pinned):
        notes.append(f"note: pinned_period {pinned} is not closed — goldens skipped")
        specs = []
    if specs and goldens is None:
        fail("case has goldens but no golden runner is available (see --golden)")
        specs = []
    for g in specs:
        try:
            rows, how = goldens.rows(g["sql"])
        except Exception as e:  # noqa: BLE001 — a golden that cannot run is a failure, not a pass
            fail(f"golden [{g['judge']}] could not be computed: {str(e)[:160]}")
            continue
        passed, note = eval_judges.judge(g["judge"], answer, rows, float(g["rel_tol"]))
        if passed:
            notes.append(f"golden {g['judge']} ok ({how}): {note}")
        else:
            fail(f"golden [{g['judge']}] ({how}): {note}")
    if n := expect.get("max_tool_calls"):
        got = len(_sqls(turns))
        if got > n:
            fail(f"tool calls {got} > {n}")
    if ms := expect.get("followup_engine_ms_max"):
        late = [e.get("engine_ms", 0) for e in _phases(turns, "execute", from_turn=1)]
        if late and max(late) > ms:
            fail(f"follow-up engine_ms max {max(late)} > {ms}")
    if pat := expect.get("followup_sql_regex_any"):
        if not any(re.search(pat, q, re.I | re.S) for q in _sqls(turns, from_turn=1)):
            fail(f"no follow-up SQL matches /{pat}/")
    return ok, notes


# ------------------------------------------------------------------ report
def summarize(case: dict, turns: list[Turn], ok: bool, notes: list[str], tag: str = "") -> dict:
    return {
        "id": case["id"],
        "category": case["category"],
        "persona": case.get("persona", "admin"),
        "passed": ok,
        "notes": notes,
        "tool_calls": len(_sqls(turns)),
        "engine_ms_total": round(sum(e.get("engine_ms", 0) for e in _phases(turns, "execute")), 1),
        "wall_s": round(sum(t.wall_s for t in turns), 1),
        "input_tokens": sum(int(t.usage.get("input_tokens") or 0) for t in turns),
        "output_tokens": sum(int(t.usage.get("output_tokens") or 0) for t in turns),
        "model_calls": sum(int(t.usage.get("model_calls") or 0) for t in turns),
        "tag": tag,
        "rejections": _rejections(turns)[:3],
        "sql": [q[:200] for q in _sqls(turns)],
        "answer": "\n".join(t.answer for t in turns)[:800],
    }


# Model answers quote whatever the data holds — CloudTrail principal ids, access
# key ids. They are identifiers, not credentials, but they look exactly like
# AWS keys to secret scanners (and the repo is public-hygiene by policy), so
# every string in a report row is redacted before it is written.
_AWS_ID_RE = re.compile(r"\b(?:AROA|AKIA|ASIA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16,}\b")
# 12-digit account ids: identifiers, not secrets, but a published report should
# not carry the deployer's account (public-hygiene rule)
_ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _redact(obj):
    if isinstance(obj, str):
        return _ACCOUNT_RE.sub("<account-id>", _AWS_ID_RE.sub("<aws-id-redacted>", obj))
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def write_report(rows: list[dict], mode: str, out_md: Path, header_note: str = "") -> None:
    rows = _redact(rows)
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%MZ")
    lines = [
        f"# Eval results — mode `{mode}` · {stamp}",
        "",
        f"{sum(r['passed'] for r in rows)}/{len(rows)} cases passed. "
        "Assertions run on the streamed gate → rewrite → cost → execute trace events, "
        "not on output text; numeric truths are computed at eval time by "
        "running each case's golden SQL through the same pipeline.",
    ]
    if header_note:
        lines += ["", header_note]
    lines += [
        "",
        "| Category | Cases | Passed | Pass rate | Mean SQL calls | Mean input tokens |",
        "|---|---|---|---|---|---|",
    ]
    for cat, rs in sorted(by_cat.items()):
        p = sum(r["passed"] for r in rs)
        lines.append(
            f"| {cat} | {len(rs)} | {p} | {p / len(rs):.0%} | "
            f"{_mean([r['tool_calls'] for r in rs])} | "
            f"{_mean([r['input_tokens'] for r in rs]):,.0f} |"
        )
    lines += [
        "",
        "| Case | Persona | Result | SQL calls | Engine ms | Input tokens | Model calls "
        "| Wall s | Notes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        note = "; ".join(r["notes"])[:200] if r["notes"] else ""
        lines.append(
            f"| {r['id']} | {r['persona']} | {'✅' if r['passed'] else '❌'} | {r['tool_calls']} | "
            f"{r['engine_ms_total']} | {r['input_tokens']:,} | {r['model_calls']} | {r['wall_s']} "
            f"| {note} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_md.with_suffix(".json").write_text(
        json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8"
    )


# ------------------------------------------------------- bandwidth gate
def _band_pp(baseline_total: int) -> float:
    """Allowed downward drift in percentage points, by category size: a large
    category gets a tight band, a small one a wide band — but every band is
    narrower than one case flipping, so a single regression in any current
    category turns the gate red."""
    if baseline_total >= 50:
        return 2.0
    if baseline_total >= 10:
        return 5.0
    return 10.0


def load_baseline() -> dict | None:
    try:
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_baseline(rows: list[dict], mode: str) -> None:
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    BASELINE_FILE.write_text(
        json.dumps(
            {
                "as_of": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%MZ"),
                "mode": mode,
                "categories": {
                    cat: {"passed": sum(r["passed"] for r in rs), "total": len(rs)}
                    for cat, rs in sorted(by_cat.items())
                },
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


def gate_against_baseline(rows: list[dict], baseline: dict) -> tuple[list[str], list[str]]:
    """Compare this run's per-category pass rates to the locked baseline.

    Returns (breaches, notes). Only categories present in BOTH are compared, so
    a --category shard gates just what it ran; only downward drift breaches
    (an improvement is a reason to refresh the baseline, not a failure). A
    category whose case count differs from the baseline is reported as a note,
    not a breach — the baseline is stale, refresh it with --update-baseline."""
    breaches, notes = [], []
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, rs in sorted(by_cat.items()):
        base = (baseline.get("categories") or {}).get(cat)
        if not base:
            notes.append(f"{cat}: not in the baseline — refresh with --update-baseline")
            continue
        if len(rs) != base["total"]:
            notes.append(
                f"{cat}: {len(rs)} cases vs {base['total']} in the baseline — "
                "stale baseline, refresh with --update-baseline"
            )
            continue
        rate = 100.0 * sum(r["passed"] for r in rs) / len(rs)
        base_rate = 100.0 * base["passed"] / base["total"]
        band = _band_pp(base["total"])
        if rate < base_rate - band:
            breaches.append(
                f"{cat}: {rate:.0f}% vs baseline {base_rate:.0f}% "
                f"(-{base_rate - rate:.0f} pp, band ±{band:.0f} pp)"
            )
    return breaches, notes


# -------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=["local", "cloud"], default="cloud")
    ap.add_argument("--only", default="", help="comma-separated case ids")
    ap.add_argument("--category", default="")
    ap.add_argument("--out", default=str(ROOT / "docs" / "eval-results.md"))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--golden",
        choices=["auto", "compute", "cache"],
        default="auto",
        help="auto: replay cached golden rows else compute; compute: always run the golden SQL "
        "(needs S3 reach); cache: never compute (laptop without S3 reach)",
    )
    ap.add_argument("--golden-role", default="ops:admin", help="role the golden SQL runs as")
    ap.add_argument(
        "--no-search-schema",
        action="store_true",
        help="local mode: run the agent without the search_schema tool (data-map ablation)",
    )
    ap.add_argument(
        "--no-call-metric",
        action="store_true",
        help="local mode: run the agent without the call_metric tool (semantic-layer A/B)",
    )
    ap.add_argument("--tag", default="", help="free label stored on every result row")
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="write this run's per-category pass rates as the locked bandwidth baseline "
        " — pair it with `--golden compute` after a data or view change",
    )
    ap.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the bandwidth gate (one-off experiments); ablation runs skip it anyway",
    )
    args = ap.parse_args()

    personas = load_personas()
    cases = load_cases(set(filter(None, args.only.split(","))), args.category or None)
    if not cases:
        sys.exit("no cases selected")
    drop = frozenset(
        (["search_schema"] if args.no_search_schema else [])
        + (["call_metric"] if args.no_call_metric else [])
    )
    if drop and args.mode != "local":
        sys.exit("--no-search-schema / --no-call-metric are local-mode ablations")
    if args.mode == "cloud":
        # Pre-flight: the deployment must be named explicitly (no defaults), and a
        # login failure must abort the run, not become 35 fake FAILs that overwrite
        # the published results.
        missing = [
            name
            for name, ok in (
                ("AGENT_RUNTIME_ARN", bool(RUNTIME_ARN)),
                ("COGNITO_CLIENT_ID", bool(os.environ.get("COGNITO_CLIENT_ID"))),
                (
                    "COGNITO_CLIENT_SECRET (or COGNITO_CLIENT_SECRET_FILE)",
                    bool(
                        os.environ.get("COGNITO_CLIENT_SECRET")
                        or os.environ.get("COGNITO_CLIENT_SECRET_FILE")
                    ),
                ),
            )
            if not ok
        ]
        if missing:
            sys.exit("--mode cloud needs " + ", ".join(missing) + " from your deployment")
        for name in sorted({c.get("persona", "admin") for c in cases}):
            try:
                _cloud_token(personas[name]["user"])
            except Exception as e:  # noqa: BLE001 — report and stop
                sys.exit(f"pre-flight login failed for persona {name!r}: {e}")
    goldens = GoldenRunner(args.golden, args.golden_role)
    tag = args.tag or ("no-" + "-no-".join(sorted(drop)) if drop else "")
    rows = []
    for case in cases:
        persona = personas[case.get("persona", "admin")]
        prompts = case.get("prompts") or [case["prompt"]]
        print(
            f"▶ {case['id']} [{case['category']}] as {case.get('persona', 'admin')} …", flush=True
        )
        needs = case.get("requires_env") or {}
        skipped = ""
        if needs and args.mode == "cloud":
            missing = {k: v for k, v in needs.items() if os.environ.get(f"RUNTIME_{k}") != str(v)}
            if missing:
                skipped = (
                    f"skipped in cloud mode: needs runtime env {needs} "
                    "(deploy with those values, or run this case with --mode local)"
                )
        source = case.get("requires_source")
        deployed = os.environ.get("EVAL_DEPLOYED_SOURCES")
        if source and args.mode == "cloud" and deployed is not None:
            prefixes = [p.strip() for p in deployed.split(",") if p.strip()]
            if not any(source.startswith(p) for p in prefixes):
                skipped = (
                    f"skipped in cloud mode: needs source tool {source!r}, not in "
                    f"EVAL_DEPLOYED_SOURCES={deployed!r} (deploy the source, or unset the variable)"
                )
        if skipped:
            rows.append(summarize(case, [Turn()], True, [f"note: {skipped}"], tag))
            print(f"   SKIP · {skipped}")
            continue
        saved_env = {k: os.environ.get(k) for k in needs}
        try:
            for k, v in needs.items():
                os.environ[k] = str(v)
            if needs:
                _reload_env_knobs()
            if args.mode == "local":
                turns = run_local(prompts, persona["role"], drop)
            else:
                turns = run_cloud(prompts, persona["user"], bool(case.get("session_break", False)))
        except Exception as e:  # noqa: BLE001 — record and continue
            turns = [Turn()]
            turns[0].text.append(f"[runner error] {e}")
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if needs:
                _reload_env_knobs()
        ok, notes = evaluate(case.get("expect", {}), turns, goldens, case.get("pinned_period"))
        row = summarize(case, turns, ok, notes, tag)
        rows.append(row)
        verdict = "PASS" if ok else "FAIL"
        print(
            f"   {verdict} · {row['tool_calls']} SQL · {row['engine_ms_total']} ms engine"
            f" · {row['input_tokens']:,} in-tokens · {row['model_calls']} calls · {row['wall_s']} s"
        )
        for n in notes:
            print("   ", n)
        if args.verbose:
            for t in turns:
                for e in t.trace:
                    print("     ", json.dumps(e)[:200])
            print("     answer:", row["answer"][:400].replace("\n", " "))
    goldens.save()
    note = (
        f"Goldens: {goldens.computed} computed at eval time, {goldens.replayed} replayed from "
        f"cache (role `{goldens.role}`)." + (f" Tag: `{tag}`." if tag else "")
    )
    # Bandwidth gate: this run's per-category pass rates vs the locked
    # baseline. Ablation runs are experiments, not the system under watch.
    breaches: list[str] = []
    baseline = load_baseline()
    if args.update_baseline:
        save_baseline(rows, args.mode)
        note += f"\n\nBandwidth gate: baseline UPDATED → `{BASELINE_FILE.name}`."
    elif args.no_gate or drop:
        note += "\n\nBandwidth gate: skipped (ablation / --no-gate)."
    elif baseline is None:
        note += "\n\nBandwidth gate: no baseline yet — run with --update-baseline."
    else:
        breaches, gate_notes = gate_against_baseline(rows, baseline)
        status = "BREACH — " + "; ".join(breaches) if breaches else "PASS"
        note += (
            f"\n\nBandwidth gate: **{status}** vs baseline {baseline.get('as_of')} "
            "(bands ±2 pp ≥50 cases, ±5 pp ≥10, ±10 pp below — every band is narrower "
            "than one case flipping)."
        )
        if gate_notes:
            note += " Notes: " + "; ".join(gate_notes) + "."
    write_report(rows, args.mode, Path(args.out), note)
    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed → {args.out}")
    if breaches:
        print("bandwidth gate BREACH:", "; ".join(breaches))
        sys.exit(2)
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
