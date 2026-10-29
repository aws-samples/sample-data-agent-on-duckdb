"""Lake Formation as the policy authority — the fetched per-persona view
is compiled into the session policy the rewrite already enforces, merged with
the env policy taking the stricter, and ANY fetch failure is a table-level
deny, never a fallback to the env rule alone."""

import pytest

from data_agent import governance, pipeline
from data_agent.governance import lakeformation as lf
from data_agent.governance import policies

ROLE = "exchange-a:junior"
CFG = {
    "session_tag": "data-agent",
    "roles": {ROLE: "arn:aws:iam::123456789012:role/lf-persona-junior"},
    "tables": {
        "eth_traces": {
            "catalog_id": "123456789012:s3tablescatalog/demo",
            "database": "blockchain",
            "name": "eth_traces",
        }
    },
}


@pytest.fixture(autouse=True)
def _lf_env(monkeypatch):
    monkeypatch.setattr(lf, "CONFIG", CFG)
    lf.reset()
    yield
    lf.reset()
    policies.set_session_policy(None)


def _rule(row_filter=None, deny=()):
    def fake_build(role):
        base = {
            k: (dict(v) if isinstance(v, dict) else v) for k, v in policies.POLICIES[role].items()
        }
        base["tables"] = dict(base["tables"])
        merged = lf._merge_rule(
            base["tables"].get("eth_traces") or {},
            {"row_filter": row_filter, "deny_columns": list(deny)},
        )
        base["tables"]["eth_traces"] = merged
        return base, [f"eth_traces: rls=yes cls={len(deny)}"]

    return fake_build


# ---------------------------------------------------------------- merge rules
def test_merge_ands_row_filters_and_unions_denies():
    env = {"row_filter": "date >= '2025-02-21'", "deny_columns": ["subject_identity"]}
    out = lf._merge_rule(env, {"row_filter": "exchange = 'a'", "deny_columns": ["block_timestamp"]})
    assert out["row_filter"] == "(date >= '2025-02-21') AND (exchange = 'a')"
    assert out["deny_columns"] == ["subject_identity", "block_timestamp"]


def test_merge_keeps_a_single_side_and_dedups_identical_filters():
    assert lf._merge_rule({}, {"row_filter": "a = 1", "deny_columns": []}) == {
        "row_filter": "a = 1"
    }
    assert lf._merge_rule({"row_filter": "a = 1"}, {"row_filter": "a = 1", "deny_columns": []}) == {
        "row_filter": "a = 1"
    }
    assert lf._merge_rule({}, {"row_filter": None, "deny_columns": []}) == {}


# ------------------------------------------------------- bind installs overlay
def test_bind_installs_the_merged_session_policy(monkeypatch):
    monkeypatch.setattr(lf, "build_session_policy", _rule("value > 0", ["block_timestamp"]))
    governance.set_active_role(ROLE)
    notes = lf.bind(ROLE)
    assert notes and "rls=yes" in notes[0]
    rule = governance.active_policy()["tables"]["eth_traces"]
    assert "(date >= '2025-02-21') AND (value > 0)" == rule["row_filter"]
    assert "block_timestamp" in rule["deny_columns"]
    # the overlay feeds every reader: denied_columns sees the LF column
    assert "block_timestamp" in governance.denied_columns()["eth_traces"]


def test_the_overlay_dies_with_the_role(monkeypatch):
    monkeypatch.setattr(lf, "build_session_policy", _rule("value > 0"))
    governance.set_active_role(ROLE)
    lf.bind(ROLE)
    assert "value > 0" in governance.active_policy()["tables"]["eth_traces"]["row_filter"]
    governance.set_active_role("ops:finops")  # role change clears the overlay
    assert "eth_traces" not in (governance.active_policy().get("tables") or {})


def test_a_fetch_failure_is_a_table_level_deny(monkeypatch):
    def failing(role):
        base = {k: v for k, v in policies.POLICIES[role].items()}
        base["tables"] = {k: v for k, v in base["tables"].items() if k != "eth_traces"}
        return base, ["eth_traces: DENIED (boom)"]

    monkeypatch.setattr(lf, "build_session_policy", failing)
    governance.set_active_role(ROLE)
    notes = lf.bind(ROLE)
    assert any("DENIED" in n for n in notes)
    assert "eth_traces" not in governance.active_policy()["tables"]
    # and the pipeline refuses it exactly like any unlisted table
    out = pipeline.run_text("SELECT count(*) FROM eth_traces WHERE date = '2025-02-21'")
    assert out.startswith("ERROR") and "not in the access policy" in out


def test_bind_is_cached_per_role(monkeypatch):
    calls = []

    def counting(role):
        calls.append(role)
        return _rule("value > 0")(role)

    monkeypatch.setattr(lf, "build_session_policy", counting)
    governance.set_active_role(ROLE)
    lf.bind(ROLE)
    lf.bind(ROLE)
    assert calls == [ROLE]  # second bind served from cache


def test_personas_outside_the_config_are_untouched():
    governance.set_active_role("ops:finops")
    assert lf.bind("ops:finops") == []
    assert governance.active_policy() is policies.POLICIES["ops:finops"]


# ------------------------------------------------------------ rule compilation
class _FakeGlue:
    def __init__(self, authorized, filters, cols=("date", "value", "block_timestamp")):
        self._out = {
            "Table": {"StorageDescriptor": {"Columns": [{"Name": c} for c in cols]}},
            "AuthorizedColumns": authorized,
            "CellFilters": filters,
        }

    def get_unfiltered_table_metadata(self, **kw):
        return self._out


LOCUS = CFG["tables"]["eth_traces"]


def test_fetch_compiles_authorized_columns_and_one_predicate():
    glue = _FakeGlue(
        ["date", "value"], [{"ColumnName": "date", "RowFilterExpression": "value > 0"}]
    )
    rule = lf._fetch_table_rule(glue, LOCUS)
    assert rule == {"row_filter": "value > 0", "deny_columns": ["block_timestamp"]}


def test_true_filter_means_no_row_filter():
    glue = _FakeGlue(
        ["date", "value", "block_timestamp"],
        [{"ColumnName": "date", "RowFilterExpression": "TRUE"}],
    )
    assert lf._fetch_table_rule(glue, LOCUS) == {"row_filter": None, "deny_columns": []}


def test_cell_level_filters_fail_closed():
    glue = _FakeGlue(
        ["date", "value"],
        [
            {"ColumnName": "date", "RowFilterExpression": "value > 0"},
            {"ColumnName": "value", "RowFilterExpression": "value > 1"},
        ],
    )
    with pytest.raises(ValueError, match="cell-level"):
        lf._fetch_table_rule(glue, LOCUS)


def test_an_unparseable_partiql_predicate_fails_closed():
    glue = _FakeGlue(["date"], [{"ColumnName": "date", "RowFilterExpression": "value >>> ???"}])
    with pytest.raises(Exception):  # noqa: B017 — any parse failure must refuse
        lf._fetch_table_rule(glue, LOCUS)


def test_no_authorized_columns_fails_closed():
    glue = _FakeGlue([], [])
    with pytest.raises(ValueError, match="no authorized columns"):
        lf._fetch_table_rule(glue, LOCUS)


# ---------------------------------------------------------------- config shape
@pytest.mark.parametrize(
    ("cfg", "match"),
    [
        ('{"session_tag": ""}', "session_tag"),
        ('{"session_tag": "t", "roles": {"a:b": "not-an-arn"}}', "IAM role ARN"),
        ('{"session_tag": "t", "tables": {"x": {"database": "d"}}}', "catalog_id"),
        ('{"session_tag": "t", "surprise": 1}', "unknown keys"),
        ("not json", "does not parse"),
    ],
)
def test_malformed_config_fails_stop(monkeypatch, cfg, match):
    monkeypatch.setenv("LF_GOVERNANCE", cfg)
    with pytest.raises(lf.LFConfigError, match=match):
        lf._load_config()


def test_absent_config_disables_cleanly(monkeypatch):
    monkeypatch.delenv("LF_GOVERNANCE", raising=False)
    assert lf._load_config() is None
    monkeypatch.setattr(lf, "CONFIG", None)
    assert lf.bind(ROLE) == [] and not lf.enabled_for(ROLE)
