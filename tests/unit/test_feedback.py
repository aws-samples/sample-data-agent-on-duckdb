"""Online feedback bucketed by provenance tier. A verdict names a
turn of THIS session; its bucket is the most deterministic tier the turn's
executed statements used; it is one EMF line (Feedback / FeedbackNegative by
Provenance) and never reaches the model."""

import json

import pytest

import agent as agent_mod
from data_agent import audit, feedback, pipeline
from data_agent.scheduling import runtime
from tests.unit.test_agent import _drain


@pytest.fixture(autouse=True)
def _fresh():
    audit.reset_session()
    runtime._session["owner"] = None
    yield
    audit.reset_session()


def _lines(capsys):
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln]


def test_bucket_takes_the_most_deterministic_tier_the_turn_used():
    assert feedback.bucket(["free_sql", "verified", "free_sql"]) == "verified"
    assert feedback.bucket(["free_sql", "metric"]) == "metric"
    assert feedback.bucket(["free_sql"]) == "free_sql"
    assert feedback.bucket([]) == "none"


def test_a_turn_collects_the_provenance_of_its_executed_statements(local_conn):
    tid = audit.begin_turn("how many rows?")
    pipeline.run_text("SELECT count(*) FROM t")
    meta = {"name": "m", "version": "1.0", "owner": "o", "table": "t"}
    list(pipeline.run("SELECT 1", metric=meta))
    pipeline.run_text("DROP TABLE t")  # rejected: does not count toward the turn
    _, turn = audit.turn(tid)
    assert turn["provenance"] == ["free_sql", "metric"]
    assert turn["question"] == "how many rows?"
    tid2 = audit.begin_turn("next")
    assert audit.turn()[0] == tid2 and audit.turn(tid)[1] is not None  # older turns kept


def test_feedback_is_one_emf_line_with_the_turn_bucket(local_conn, capsys):
    tid = audit.begin_turn("q")
    meta = {"name": "vq", "version": "1.0", "owner": "o", "table": "t"}
    list(pipeline.run("SELECT 1", metric=meta, provenance="verified"))
    capsys.readouterr()
    out = feedback.record(
        {"verdict": "down", "correction": "  the window is wrong  ", "turn_id": tid},
        principal="ops:admin",
        user_sub="abc",
    )
    assert out == {
        "verdict": "down",
        "provenance": "verified",
        "turn_id": tid,
        "matched_turn": True,
        "correction_recorded": True,
    }
    rec = _lines(capsys)[-1]
    assert rec["Provenance"] == "verified" and rec["FeedbackNegative"] == 1
    assert rec["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Provenance"]]
    a = rec["audit"]
    assert a["stage"] == "feedback" and a["correction"] == "the window is wrong"
    assert a["question"] == "q" and a["provenance_mix"] == ["verified"]
    assert audit.session_counters()["statements"] == 1  # feedback is not a statement


@pytest.mark.parametrize(
    "bad, match",
    [
        ({"verdict": "meh"}, "verdict"),
        ({"verdict": "up", "correction": 5}, "text"),
        ("down", "object"),
    ],
)
def test_a_malformed_feedback_is_refused(bad, match):
    with pytest.raises(feedback.FeedbackError, match=match):
        feedback.record(bad, principal="ops:admin", user_sub=None)


def test_feedback_on_an_unknown_turn_is_recorded_as_unmatched(capsys):
    out = feedback.record({"verdict": "up", "turn_id": "nope"}, principal="p", user_sub=None)
    assert out["matched_turn"] is False and out["provenance"] == "none"


def test_the_entrypoint_records_feedback_without_running_the_model(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(agent_mod.agent, "stream_async", lambda *_a, **_k: called.append(1))
    ident = {"tenant": "ops", "role": "admin", "sub": "u-1"}
    tid = audit.begin_turn("earlier question")
    events = _drain(
        agent_mod.invoke(
            {"identity": ident, "feedback": {"verdict": "down", "turn_id": tid}}, context=None
        )
    )
    assert not called
    assert [e["kind"] for e in events] == ["start", "text", "result"]
    assert events[-1]["feedback"]["turn_id"] == tid
    assert any(ln["audit"].get("stage") == "feedback" for ln in _lines(capsys))
    bad = _drain(agent_mod.invoke({"identity": ident, "feedback": {"verdict": "?"}}, None))
    assert "not recorded" in bad[1]["text"] and bad[-1]["feedback"] is None
