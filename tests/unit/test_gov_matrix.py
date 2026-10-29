"""Governance coverage matrix as CI: every persona × access path ×
statement shape cell runs through the real pipeline on a local engine shaped
like the deployed catalog (built from the table cards). Zero network, zero
tokens. Adding a governed table or a persona adds cells automatically.

All six paths run here: the four S3 access methods, the MCP surface (role from a
token) and the tool path (a Gateway tool result landed as
an owner-bound handle and joined with the governed table)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import gov_matrix as gm  # noqa: E402

from data_agent import governance  # noqa: E402
from data_agent.compute import engine  # noqa: E402
from data_agent.context import cost  # noqa: E402

CELLS = gm.cells(gm.EVERY_PATH)


@pytest.fixture(scope="module")
def matrix_engine():
    conn = gm.build_local_engine()
    yield conn
    conn.close()


@pytest.fixture()
def matrix_env(matrix_engine, monkeypatch):
    monkeypatch.setattr(engine, "get_conn", lambda: matrix_engine)
    monkeypatch.setattr(cost, "SOURCES", {})  # local views are not S3 prefixes
    governance.set_schema_prober(engine.probe_columns)
    yield matrix_engine


def test_matrix_shape_is_six_personas_by_six_paths_by_five_shapes_plus_deny_cells():
    core = [c for c in CELLS if c.shape != "deny"]
    assert len(core) == 6 * 6 * 5 == 180
    assert len(gm.cells(gm.PRIMARY_PATHS)) == 6 * 2 * 5 + 5  # main line + 5 deny cells
    assert len(gm.cells()) == 6 * 4 * 5 + 5  # the four S3 access methods
    deny = [c for c in CELLS if c.shape == "deny" and c.path == "raw"]
    assert {c.table for c in deny} == {"cw_logs"}  # admin-only by omission, every persona
    assert {c.expected for c in deny} == {"rejected"}
    assert len(deny) == 5  # admin has no unlisted table
    # the tool path: every persona joins its home table with a landed handle, and
    # EVERY persona (admin too) gets one foreign-handle cell — ownership is session
    # binding, not policy
    tool = [c for c in CELLS if c.path == gm.TOOL_PATH and c.shape != "deny"]
    assert len(tool) == 6 * 5 and all(gm.HANDLE in c.sql and " JOIN " in c.sql for c in tool)
    assert all(c.expected == "governed" for c in tool if c.persona != "admin")
    foreign = [c for c in CELLS if c.expected == "foreign_handle"]
    assert len(foreign) == 6 and all(c.path == gm.TOOL_PATH for c in foreign)
    # Every restricted persona gets one source-denied cell — a gateway
    # source outside its `sources` allowlist is refused before any outbound call
    src = [c for c in CELLS if c.expected == "source_denied"]
    assert len(src) == 5 and all(c.path == gm.TOOL_PATH and c.shape == "deny" for c in src)
    assert {c.persona for c in src} == set(gm.personas()) - {"admin"}
    # the denied source is derived from the policy: never one the role holds
    for c in src:
        assert not any(
            c.ref == s or c.ref.startswith(f"{s}___")
            for s in gm.policy_of(c.role).get("sources") or ()
        )
    # expectations are derived from the policies: the ops personas may not
    # reach the on-chain slice through any catalog leg, the exchange personas may
    legs = ("s3t", "glue", "dl")
    assert all(c.expected == "rejected" for c in CELLS if c.persona == "finops" and c.path in legs)
    assert all(
        c.expected == "governed" and c.expect_rls
        for c in CELLS
        if c.persona in ("investigator", "junior") and c.path in legs
    )
    assert all(
        c.expected == "ungoverned" for c in CELLS if c.persona == "admin" and c.shape != "deny"
    )


@pytest.mark.parametrize("cell", CELLS, ids=[c.id for c in CELLS])
def test_cell(cell, matrix_env):
    verdict = gm.run_cell(cell)
    assert verdict["ok"], verdict["problems"]


def test_a_new_governed_table_extends_the_matrix(monkeypatch):
    """Adding a table to a persona's policy adds its cells — no list to update."""
    pol = {k: {**v, "tables": dict(v.get("tables") or {})} for k, v in governance.POLICIES.items()}
    pol["ops:finops"]["tables"]["cur_line_items"] = {"row_filter": "x = 'y'", "deny_columns": []}
    pol["ops:finops"]["tables"]["new_table"] = {"row_filter": "a = 'b'", "deny_columns": ["c", "d"]}
    monkeypatch.setattr(governance.policies, "POLICIES", pol)
    home = gm.home_table("ops:finops")
    assert home == "new_table"  # the most restricted table becomes the persona's raw cell
    exp = gm.expected_for("ops:finops", "new_table")
    assert exp == ("governed", True, True)
