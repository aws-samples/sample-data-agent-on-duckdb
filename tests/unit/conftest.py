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
    # No-network guarantee: a test that reaches `engine.get_conn()` without the
    # `local_conn` fixture (e.g. governance's CLS schema prober) must never
    # trigger the real init — mounting the S3 views LISTs the public dataset
    # and hangs the suite off-VPC. Pre-seed a plain in-memory connection; a
    # test that wants the real init path calls `engine.reset()` first (and
    # stubs `_s3_secrets`), exactly as test_ops does.
    monkeypatch.setattr(engine, "_conn", duckdb.connect())
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
    # same extension location as the engine (LOAD-only, no INSTALL), so
    # `LOAD fts` in the data map finds the pre-installed extension here too
    conn.execute("SET home_directory='/tmp'")
    if engine.EXTENSION_DIR:
        conn.execute(f"SET extension_directory='{engine.EXTENSION_DIR}'")
    conn.execute("CREATE TABLE t AS SELECT range AS id, 'x' || range AS name FROM range(500)")
    monkeypatch.setattr(engine, "get_conn", lambda: conn)
    yield conn
    conn.close()
