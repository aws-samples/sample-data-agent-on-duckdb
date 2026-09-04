"""Universal hardening — rules that apply to EVERY principal, the
unrestricted role included. Each parametrized case was a verified bypass
before the fix (see data_agent/governance/hardening.py)."""

import pytest

from data_agent import governance
from data_agent.pipeline import run_text as run_sql

JUNIOR = "exchange-a:junior"


# ------------------------------------------------ CTE-prefixed DML (any role)
@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1 AS a) INSERT INTO t SELECT a, 'z' FROM x",
        "WITH x AS (SELECT 1) DELETE FROM t WHERE id = 0",
        "WITH x AS (SELECT 1) UPDATE t SET name = 'p' WHERE id = 1",
    ],
)
def test_cte_prefixed_dml_is_rejected_for_the_unrestricted_role(sql, local_conn):
    """The read-only gate classifies on the leading keyword ('with'); before
    the fix these executed for ops:admin (INSERT and DELETE verified live)."""
    out = run_sql(sql)
    assert out.startswith("ERROR: governance: statement type")
    assert local_conn.execute("SELECT count(*) FROM t").fetchone()[0] == 500  # untouched


# ---------------------------------------------- banned functions (any role)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_secrets()",
        "SELECT which_secret('s3://aws-public-blockchain/x', 's3')",
        "SELECT content FROM read_text('/etc/hosts')",
        "SELECT * FROM read_blob('/proc/self/environ')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT getenv('AWS_SECRET_ACCESS_KEY')",
        "SELECT * FROM t WHERE name = (SELECT content FROM read_text('/etc/hosts'))",
    ],
)
@pytest.mark.parametrize("role", ["ops:admin", JUNIOR])
def test_host_reaching_functions_are_rejected_for_every_role(sql, role, local_conn):
    governance.set_active_role(role)
    out = run_sql(sql)
    assert out.startswith("ERROR: governance: function"), out
    assert "/etc" not in out.split("—")[0] or "not allowed" in out


def test_banned_function_ban_survives_unparseable_sql():
    """The keyword-level check runs before parsing, so DuckDB-only syntax
    sqlglot cannot model still cannot smuggle a banned function."""
    with pytest.raises(ValueError, match="function 'read_text' is not allowed"):
        governance.apply_governance("FROM read_text('/etc/hosts') SELECT content")


# ------------------------------------------------- path schemes (any role)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM '/etc/passwd'",  # replacement scan via a bare string
        "SELECT * FROM 'file:///etc/passwd'",
        "SELECT * FROM read_parquet('/tmp/x.parquet')",
        "SELECT * FROM read_csv('https://169.254.170.2/v2/credentials', header=false)",
        "SELECT * FROM read_json_auto('http://evil.example/x.json')",
        "SELECT * FROM read_parquet(['s3://ok/a.parquet', '/tmp/b.parquet'])",
    ],
)
def test_non_s3_paths_are_rejected_even_for_the_unrestricted_role(sql):
    with pytest.raises(ValueError, match="not under an allowed scheme"):
        governance.apply_governance(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('s3://aws-public-blockchain/v1.0/eth/traces/date=2025-02-21/*.parquet')",
        "SELECT * FROM 's3://aws-public-blockchain/v1.0/eth/traces/date=2025-02-21/part-0.parquet'",
        "SELECT * FROM read_parquet(['s3://a/x.parquet', 's3://a/y.parquet'], hive_partitioning=1)",
        "SELECT * FROM read_csv('s3://b/c.csv', delim=',', header=true)",  # non-path literals pass
    ],
)
def test_s3_paths_stay_allowed_for_the_unrestricted_role(sql):
    assert governance.apply_governance(sql) == sql


def test_path_like_identifier_counts_as_raw_access_for_restricted_roles():
    governance.set_active_role(JUNIOR)
    with pytest.raises(ValueError, match="raw file access via path"):
        governance.apply_governance(
            "SELECT * FROM 's3://aws-public-blockchain/v1.0/eth/traces/date=2025-02-21/part-0.parquet'"
        )


def test_ordinary_names_are_not_mistaken_for_paths(local_conn):
    """Dots in qualified names and plain identifiers are not path references."""
    assert governance.apply_governance("SELECT * FROM t") == "SELECT * FROM t"
    assert not run_sql("SELECT count(*) FROM t").startswith("ERROR")
    assert governance.apply_governance("SELECT * FROM main.t") == "SELECT * FROM main.t"


def test_explain_still_available_to_the_unrestricted_role_after_hardening():
    assert governance.apply_governance("EXPLAIN SELECT 1") == "EXPLAIN SELECT 1"
