"""Measured-pressure compression with engine-pointer stubs."""

import json
from types import SimpleNamespace

import pytest

from data_agent.context import compression as context
from data_agent.context.compression import STUB_PREFIX, ResultStubConversationManager


# ---------------------------------------------------------------- helpers
def _use(i: int, sql: str) -> dict:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": f"u{i}", "name": "run_sql", "input": {"sql": sql}}}],
    }


def _result(i: int, text: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": f"u{i}", "status": "success", "content": [{"text": text}]}}
        ],
    }


def _summary_text(ref: str, rows: int = 5000, pad: int = 3000) -> str:
    """What results.format_result emits for a materialized result (padded)."""
    line1 = json.dumps({"shape": "summary", "ref": ref, "rows": rows, "sample": ["x" * pad]})
    line2 = json.dumps(
        {"engine_ms": 12.0, "rows": rows, "governed": False, "shape": "summary", "ref": ref}
    )
    return f"{line1}\n{line2}\n[Result not shown inline …]"


def _inline_text(rows: int = 3, pad: int = 600) -> str:
    return (
        json.dumps([{"v": "y" * pad}] * rows)
        + "\n"
        + json.dumps({"engine_ms": 1.0, "rows": rows, "governed": False, "shape": "rows"})
    )


def _conversation(
    n_results: int = 4, inline_at: set[int] = frozenset(), sqls: dict | None = None
) -> list:
    msgs = [{"role": "user", "content": [{"text": "trace the exploiter"}]}]
    for i in range(1, n_results + 1):
        sql = (sqls or {}).get(i, f"SELECT * FROM eth_traces WHERE hop = {i}")
        msgs.append(_use(i, sql))
        msgs.append(_result(i, _inline_text() if i in inline_at else _summary_text(f"_r_{i}")))
    # the next turn has begun: assistant summary + the user's next question, so the
    # protected tail (last 2 messages) holds no tool result
    msgs.append({"role": "assistant", "content": [{"text": "so far so good"}]})
    msgs.append({"role": "user", "content": [{"text": "and then?"}]})
    return msgs


def _agent(messages: list, window: int = 200_000):
    model = SimpleNamespace(context_window_limit=window, estimate_utilization=lambda t: t / window)
    return SimpleNamespace(messages=messages, model=model)


def _texts(messages: list) -> list[str]:
    out = []
    for m in messages:
        for b in m.get("content", []):
            if "toolResult" in b:
                out.append(context._text_of(b))
    return out


# ------------------------------------------------------------------ tests
def test_manager_is_wired_into_the_agent_with_window_150_and_no_truncation():
    import agent as agent_mod

    cm = agent_mod.agent.conversation_manager
    assert isinstance(cm, ResultStubConversationManager)
    assert cm.window_size == context.MAX_CONTEXT_MESSAGES == 150
    assert cm.should_truncate_results is False  # invariant 9


def test_below_min_messages_nothing_is_stubbed():
    msgs = _conversation(n_results=1)  # 4 messages < MIN_MESSAGES_FOR_STUB
    cm = ResultStubConversationManager()
    cm._projected = 190_000
    cm.reduce_context(_agent(msgs))
    assert not any(t.startswith(STUB_PREFIX) for t in _texts(msgs))


def test_proactive_stubs_oldest_materialized_results_up_to_the_target_only():
    msgs = _conversation(n_results=4)  # results _r_1.._r_4, each ~3KB
    cm = ResultStubConversationManager()
    cm._projected = int(0.6 * 200_000) + 1000  # 1000 tokens over → ~4000 bytes to reclaim
    cm.reduce_context(_agent(msgs))
    texts = _texts(msgs)
    # two stubs reclaim ~6KB ≥ 4KB target; the two newest results stay whole
    assert [t.startswith(STUB_PREFIX) for t in texts] == [True, True, False, False]
    assert "_r_1" in texts[0] and "kept whole in temp table _r_1" in texts[0]
    assert "SELECT * FROM eth_traces WHERE hop = 1" in texts[0]
    assert cm.stubbed == 2 and cm.reclaimed_bytes > 4000


def test_last_two_messages_are_never_stubbed_even_under_pressure():
    msgs = _conversation(n_results=3)
    msgs.pop()
    msgs.pop()  # end on the toolResult of u3 → it is inside the protected tail
    cm = ResultStubConversationManager()
    cm._projected = 199_000
    cm.reduce_context(_agent(msgs))
    texts = _texts(msgs)
    assert texts[0].startswith(STUB_PREFIX) and texts[1].startswith(STUB_PREFIX)
    assert not texts[2].startswith(STUB_PREFIX)


def test_superseded_results_go_first_regardless_of_age():
    same = "SELECT count(*) FROM eth_traces WHERE date = '2025-02-21'"
    msgs = _conversation(n_results=4, sqls={2: same, 4: same})  # u2 is superseded by u4
    cm = ResultStubConversationManager()
    cm._projected = int(0.6 * 200_000) + 100  # tiny target → exactly one stub
    cm.reduce_context(_agent(msgs))
    texts = _texts(msgs)
    assert [t.startswith(STUB_PREFIX) for t in texts] == [False, True, False, False]


def test_inline_results_are_left_alone_proactively_but_stubbed_on_real_overflow():
    msgs = _conversation(n_results=4, inline_at={1, 2})
    cm = ResultStubConversationManager()
    cm._projected = 199_000
    cm.reduce_context(_agent(msgs))  # proactive
    texts = _texts(msgs)
    assert [t.startswith(STUB_PREFIX) for t in texts] == [False, False, True, True]
    cm.reduce_context(_agent(msgs), e=RuntimeError("Input is too long"))  # reactive
    texts = _texts(msgs)
    assert all(t.startswith(STUB_PREFIX) for t in texts)
    assert "inline result removed; re-run the SQL" in texts[0]


def test_already_stubbed_and_tiny_results_are_skipped():
    msgs = _conversation(n_results=3)
    cm = ResultStubConversationManager()
    cm.stub_results(msgs)  # everything eligible, no target
    assert cm.stubbed == 3
    n = cm.stubbed
    cm.stub_results(msgs)
    assert cm.stubbed == n  # idempotent: stubs are not re-stubbed


def test_nothing_left_to_stub_falls_back_to_window_trim():
    msgs = _conversation(n_results=3)
    cm = ResultStubConversationManager()
    cm.stub_results(msgs)
    cm.window_size = 3  # force the sliding window to do something
    before = len(msgs)
    cm.reduce_context(_agent(msgs))
    assert len(msgs) < before


def test_hook_records_the_measurement_and_triggers_at_threshold():
    msgs = _conversation(n_results=4)
    cm = ResultStubConversationManager()
    agent = _agent(msgs)
    cm._on_before_model_call_threshold(SimpleNamespace(projected_input_tokens=100_000, agent=agent))
    assert cm._projected == 100_000 and cm.stubbed == 0  # 50 % < 60 %: untouched
    cm._on_before_model_call_threshold(SimpleNamespace(projected_input_tokens=125_000, agent=agent))
    assert cm.stubbed >= 1  # 62.5 % ≥ 60 %: compressed


@pytest.mark.parametrize("text", ["", "[]\n" + json.dumps({"rows": 0, "shape": "rows"})])
def test_empty_results_are_not_candidates(text):
    msgs = [_use(1, "SELECT 1"), _result(1, text)] * 4
    cm = ResultStubConversationManager()
    assert cm.stub_results(msgs, include_inline=True) == 0
