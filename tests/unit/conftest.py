"""Shared fixtures for the unit suite (no network, no Bedrock, no S3)."""

import duckdb
import pytest

from data_agent import governance
from data_agent.compute import engine
from data_agent.context import datamap, results

UNRESTRICTED = "ops:admin"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Every test starts unrestricted (the module default is deny-all,
    see docs/design.md), with an empty result store and a data map that never probes S3."""
    governance.set_active_role(UNRESTRICTED)
    results.STORE.reset()
    datamap.reset()
    monkeypatch.setattr(datamap, "PROBE_REMOTE", False)
    # re-registering the prober clears the per-reference schema cache, so a
    # test's local table never inherits another test's columns
    governance.set_schema_prober(engine.probe_columns)
    yield
    governance.set_active_role(UNRESTRICTED)
    results.STORE.reset()


@pytest.fixture()
def local_conn(monkeypatch):
    """Swap the S3-backed connection for a plain in-memory DuckDB."""
    conn = duckdb.connect()
    conn.execute("CREATE TABLE t AS SELECT range AS id, 'x' || range AS name FROM range(500)")
    monkeypatch.setattr(engine, "get_conn", lambda: conn)
    yield conn
    conn.close()
