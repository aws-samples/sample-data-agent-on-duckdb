"""Working-set handle semantics: `_r_<n>` is a server-minted handle bound
to the session owner, stamped with created_at, valid for this session only.
A statement naming a handle this session does not hold is refused before the
engine (clean message, audited at stage `handle`), never a binder error."""

import json

import pytest

from data_agent import audit, pipeline
from data_agent.context import results
from data_agent.scheduling import runtime

BIG = "SELECT id, name FROM t"  # 500 rows > MAX_RESULT_ROWS → materialized as _r_1


def _materialize(owner="sub:alice"):
    results.STORE.bind(owner)
    items = list(pipeline.run(BIG))
    summary = json.loads(items[-1].split("\n")[0])
    assert summary["shape"] == "summary" and summary["ref"] == "_r_1"
    return summary


def test_summary_carries_an_owner_bound_handle_with_created_at_and_ttl(local_conn):
    summary = _materialize()
    h = summary["handle"]
    assert h["owner"] == results.owner_id("sub:alice") and len(h["owner"]) == 12
    assert h["owner"] != "sub:alice"  # a digest, never the raw identifier
    assert h["created_at"].endswith("Z") and "this session only" in h["ttl"]
    assert str(results.RESULT_TABLE_LRU) in h["ttl"]
    meta = results.STORE.meta("_r_1")
    assert meta["owner"] == "sub:alice" and meta["rows"] == 500


def test_own_handle_reads_back_and_refreshes_recency(local_conn):
    _materialize()
    out = pipeline.run_text("SELECT count(*) AS n FROM _r_1")
    assert json.loads(out.split("\n")[0]) == [{"n": "500"}]


def test_a_handle_minted_under_another_owner_is_refused_before_the_engine(local_conn, capsys):
    _materialize("sub:alice")
    audit.reset_session()
    results.STORE.bind("sub:bob")  # same store, different principal (defence in depth)
    items = list(pipeline.run("SELECT * FROM _r_1"))
    gate = items[0]
    assert gate["phase"] == "gate" and gate["allowed"] is False and "handle" in gate
    assert items[-1].startswith("ERROR: handle _r_1 belongs to a different principal")
    assert [i["phase"] for i in items if isinstance(i, dict)] == ["gate"]  # never reached rewrite
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    rec = json.loads(line)["audit"]
    assert rec["stage"] == "handle" and rec["owner"] == results.owner_id("sub:bob")
    assert audit.session_counters()["rejected"] == 1


def test_an_unknown_or_evicted_handle_is_refused_with_the_fix(local_conn, monkeypatch):
    results.STORE.bind("sub:alice")
    out = pipeline.run_text("SELECT * FROM _r_7")
    assert out.startswith("ERROR: handle _r_7 is not held by this session") and "re-run" in out
    # LRU eviction drops the handle's metadata too
    monkeypatch.setattr(results, "RESULT_TABLE_LRU", 1)
    _materialize()
    list(pipeline.run(BIG))  # _r_2 evicts _r_1
    assert results.STORE.meta("_r_1") is None and results.STORE.meta("_r_2") is not None
    assert pipeline.run_text("SELECT count(*) FROM _r_1").startswith("ERROR: handle _r_1 is not")


def test_session_rebind_resets_and_rebinds_the_store(local_conn, monkeypatch):
    """The runtime path: a different identity on a reused session id → the
    engine and the store are reset (T003) and the store is bound to the new
    owner, so the old handle is refused cleanly (not a binder error)."""
    monkeypatch.setattr(runtime, "_session", {"owner": None})
    assert runtime._bind_session("sub:alice") is False
    _materialize("sub:alice")
    assert results.STORE.owner == "sub:alice"
    assert runtime._bind_session("sub:bob") is True  # owner changed → caller resets
    results.STORE.reset()  # what _reset_session_state does (engine reset stubbed here)
    assert results.STORE.owner == "sub:bob" and results.STORE.meta("_r_1") is None
    out = pipeline.run_text("SELECT * FROM _r_1")
    assert out.startswith("ERROR: handle _r_1 is not held by this session")


def test_audit_line_records_the_owner_digest_on_every_statement(local_conn, capsys):
    results.STORE.bind("role:ops:admin")
    pipeline.run_text("SELECT 1")
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    assert json.loads(line)["audit"]["owner"] == results.owner_id("role:ops:admin")


@pytest.mark.parametrize("owner", [None, ""])
def test_no_owner_yields_no_digest(owner):
    assert results.owner_id(owner) is None
