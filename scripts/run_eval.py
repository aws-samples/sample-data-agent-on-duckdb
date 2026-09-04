#!/usr/bin/env python3
"""Evaluation suite runner.

Runs the yaml cases under tests/eval/cases against either the in-process
agent (`--mode local`) or the deployed AgentCore runtime through the real
Cognito → JWT → SSE path (`--mode cloud`), and asserts on the **trace
events** the agent streams (gate / rewrite / cost / execute) plus the final
answer text. Because the trace is structured, the checks are exact — "every
raw scan carried a partition filter", "the junior's rewrite hid this column",
"turn 3 hit the temp table in 4 ms" — no output-text guessing.

Not a CI gate (it spends Bedrock tokens); run it manually or nightly:

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

CASES_DIR = ROOT / "tests" / "eval" / "cases"
PERSONAS_FILE = ROOT / "tests" / "eval" / "personas.yaml"
# --mode cloud targets YOUR deployment: the runtime ARN comes from `agentcore launch`,
# the Cognito values from the CDK stack outputs. No defaults.
REGION = os.environ.get("AGENT_REGION", "us-west-2")
RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")  # required for --mode cloud
_NUM_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")


# ------------------------------------------------------------------ loading
def load_cases(only: set[str] | None, category: str | None) -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
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

    def feed(self, env: dict) -> None:
        kind = env.get("kind")
        if kind == "text":
            self.text.append(env.get("text", ""))
        elif kind == "trace":
            self.trace.append({k: v for k, v in env.items() if k != "kind"})
        elif kind in ("start", "result") and env.get("principal"):
            self.principal = env["principal"]

    @property
    def answer(self) -> str:
        return "".join(self.text)


def run_local(prompts: list[str], role: str) -> list[Turn]:
    import agent as agent_mod
    from data_agent import governance
    from data_agent.compute import engine
    from data_agent.context import datamap, prompt, results
    from data_agent.scheduling import runtime

    governance.set_active_role(role)
    agent_mod.agent.messages.clear()  # fresh conversation
    results.STORE.reset()
    datamap.reset()
    engine.reset()  # fresh engine (temp tables gone)
    prompt.reset()
    turns: list[Turn] = []

    async def _one(prompt: str) -> Turn:
        t = Turn()
        t0 = time.perf_counter()
        raw_trace: list = []
        async for ev in agent_mod.agent.stream_async(prompt):
            if not isinstance(ev, dict):
                continue
            env = runtime.map_event(ev, raw_trace)
            if env is not None:
                t.feed(env)
        t.principal = governance.active_role()
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
    vals = []
    for m in _NUM_RE.findall(text):
        try:
            vals.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return vals


def evaluate(expect: dict, turns: list[Turn]) -> tuple[bool, list[str]]:
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
        target, tol = float(spec["value"]), float(spec.get("rel_tol", 0.01))
        nums = _numbers(answer)
        if not any(abs(v - target) <= tol * abs(target) for v in nums):
            fail(f"no number within {tol:.0%} of {target} in answer (saw {nums[:8]})")
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
def summarize(case: dict, turns: list[Turn], ok: bool, notes: list[str]) -> dict:
    return {
        "id": case["id"],
        "category": case["category"],
        "persona": case.get("persona", "admin"),
        "passed": ok,
        "notes": notes,
        "tool_calls": len(_sqls(turns)),
        "engine_ms_total": round(sum(e.get("engine_ms", 0) for e in _phases(turns, "execute")), 1),
        "wall_s": round(sum(t.wall_s for t in turns), 1),
        "rejections": _rejections(turns)[:3],
        "sql": [q[:200] for q in _sqls(turns)],
        "answer": "\n".join(t.answer for t in turns)[:800],
    }


# Model answers quote whatever the data holds — CloudTrail principal ids, access
# key ids. They are identifiers, not credentials, but they look exactly like
# AWS keys to secret scanners (and the repo is public-hygiene by policy), so
# every string in a report row is redacted before it is written.
_AWS_ID_RE = re.compile(r"\b(?:AROA|AKIA|ASIA|AIDA|AGPA|ANPA|ANVA|ASCA)[0-9A-Z]{16,}\b")


def _redact(obj):
    if isinstance(obj, str):
        return _AWS_ID_RE.sub("<aws-id-redacted>", obj)
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def write_report(rows: list[dict], mode: str, out_md: Path) -> None:
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
        "not on output text.",
        "",
        "| Category | Cases | Passed | Pass rate |",
        "|---|---|---|---|",
    ]
    for cat, rs in sorted(by_cat.items()):
        p = sum(r["passed"] for r in rs)
        lines.append(f"| {cat} | {len(rs)} | {p} | {p / len(rs):.0%} |")
    lines += [
        "",
        "| Case | Persona | Result | SQL calls | Engine ms | Wall s | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        note = "; ".join(r["notes"])[:160] if r["notes"] else ""
        lines.append(
            f"| {r['id']} | {r['persona']} | {'✅' if r['passed'] else '❌'} | {r['tool_calls']} | "
            f"{r['engine_ms_total']} | {r['wall_s']} | {note} |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out_md.with_suffix(".json").write_text(
        json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8"
    )


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
    args = ap.parse_args()

    personas = load_personas()
    cases = load_cases(set(filter(None, args.only.split(","))), args.category or None)
    if not cases:
        sys.exit("no cases selected")
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
    rows = []
    for case in cases:
        persona = personas[case.get("persona", "admin")]
        prompts = case.get("prompts") or [case["prompt"]]
        print(
            f"▶ {case['id']} [{case['category']}] as {case.get('persona', 'admin')} …", flush=True
        )
        try:
            if args.mode == "local":
                turns = run_local(prompts, persona["role"])
            else:
                turns = run_cloud(prompts, persona["user"], bool(case.get("session_break", False)))
        except Exception as e:  # noqa: BLE001 — record and continue
            turns = [Turn()]
            turns[0].text.append(f"[runner error] {e}")
        ok, notes = evaluate(case.get("expect", {}), turns)
        row = summarize(case, turns, ok, notes)
        rows.append(row)
        verdict = "PASS" if ok else "FAIL"
        print(
            f"   {verdict} · {row['tool_calls']} SQL · {row['engine_ms_total']} ms engine"
            f" · {row['wall_s']} s"
        )
        for n in notes:
            print("   ", n)
        if args.verbose:
            for t in turns:
                for e in t.trace:
                    print("     ", json.dumps(e)[:200])
            print("     answer:", row["answer"][:400].replace("\n", " "))
    write_report(rows, args.mode, Path(args.out))
    passed = sum(r["passed"] for r in rows)
    print(f"\n{passed}/{len(rows)} passed → {args.out}")
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
