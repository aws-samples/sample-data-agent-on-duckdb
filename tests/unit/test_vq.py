"""Verified queries are Git assets (verified_queries/*.yaml), loaded
fail-closed exactly like metric definitions, bound as AST literals (a value
can never leave its quotes), filtered by the persona's visible tables, and
executed through the full pipeline with provenance=verified."""

import json

import pytest
import sqlglot
from sqlglot import exp

from data_agent import governance, pipeline
from data_agent.semantic import vq
from data_agent.semantic.vq import VQError


@pytest.fixture(autouse=True)
def _fresh_catalog():
    vq.reset_vqs()
    yield
    vq.reset_vqs()


# ------------------------------------------------------------ shipped asset
def test_the_shipped_onchain_asset_loads_the_three_hero_queries():
    vs = vq.load_dir(vq.assets_dir())
    assert set(vs) == {"vq_largest_inbound", "vq_fanout_recipients", "vq_cashout_to_cex"}
    assert vs["vq_cashout_to_cex"].tables == ("eth_traces", "cex_deposit_labels")
    assert all(v.version == "1.0" and v.owner and v.question for v in vs.values())
    assert all(set(v.params) == {"address", "from_date", "to_date"} for v in vs.values())


# ------------------------------------------------------------------ binding
GOOD = """
verified_queries:
  - name: v1
    version: "1.0"
    owner: o
    question: q
    tables: [t]
    params: {d: date, s: string, n: int}
    sql: SELECT id FROM t WHERE name = $s AND name > $d AND id < $n
"""


def _load(tmp_path, monkeypatch, body: str = GOOD) -> dict:
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("VERIFIED_QUERIES_DIR", str(tmp_path))
    vq.reset_vqs()
    return vq.vqs()


def test_binding_produces_literals_of_the_declared_types(tmp_path, monkeypatch):
    v = _load(tmp_path, monkeypatch)["v1"]
    sql = vq.bind_vq(v, {"d": "2025-02-21", "s": "plain", "n": 7})
    assert "'2025-02-21'" in sql and "'plain'" in sql and "< 7" in sql
    assert "$" not in sql


def test_a_hostile_string_value_stays_one_literal(tmp_path, monkeypatch):
    v = _load(tmp_path, monkeypatch)["v1"]
    hostile = "x' OR '1'='1"
    sql = vq.bind_vq(v, {"d": "2025-02-21", "s": hostile, "n": 1})
    tree = sqlglot.parse_one(sql, read="duckdb")
    strings = [lit.this for lit in tree.find_all(exp.Literal) if lit.is_string]
    assert hostile in strings  # the whole value is ONE bound literal
    assert len(list(tree.find_all(exp.Or))) == 0  # ...not a smuggled predicate


def test_badly_shaped_values_and_wrong_params_are_refused(tmp_path, monkeypatch):
    v = _load(tmp_path, monkeypatch)["v1"]
    with pytest.raises(VQError, match="does not look like a date"):
        vq.bind_vq(v, {"d": "Feb 21", "s": "x", "n": 1})
    with pytest.raises(VQError, match="must be an integer"):
        vq.bind_vq(v, {"d": "2025-02-21", "s": "x", "n": "lots"})
    with pytest.raises(VQError, match="missing"):
        vq.bind_vq(v, {"d": "2025-02-21"})
    with pytest.raises(VQError, match="unexpected"):
        vq.bind_vq(v, {"d": "2025-02-21", "s": "x", "n": 1, "evil": 1})


def test_params_may_arrive_json_encoded_like_call_metric_args(tmp_path, monkeypatch):
    v = _load(tmp_path, monkeypatch)["v1"]
    sql = vq.bind_vq(v, json.dumps({"d": "2025-02-21", "s": "x", "n": 2}))
    assert "'2025-02-21'" in sql
    with pytest.raises(VQError, match="not valid JSON"):
        vq.bind_vq(v, "{nope")


def test_an_address_parameter_is_shape_checked_and_lowercased(tmp_path, monkeypatch):
    body = GOOD.replace("params: {d: date, s: string, n: int}", "params: {a: address}").replace(
        "sql: SELECT id FROM t WHERE name = $s AND name > $d AND id < $n",
        "sql: SELECT id FROM t WHERE name = $a",
    )
    v = _load(tmp_path, monkeypatch, body)["v1"]
    assert "'0xabcd12'" in vq.bind_vq(v, {"a": "0xABCD12"})
    with pytest.raises(VQError, match="does not look like a address"):
        vq.bind_vq(v, {"a": "0x' OR 1=1"})


# ------------------------------------------------------- fail-closed loading
@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("tables: [t]", "tables: [t]\n    surprise: x", "unknown fields"),
        ("params: {d: date, s: string, n: int}", "params: {d: week, s: string, n: int}", "not in"),
        ("$n", "$undeclared", "placeholders"),
        ("FROM t", "FROM other_table", "not in tables"),
        ("name = $s", "name = ?", "must be named"),
        (
            "SELECT id FROM t WHERE name = $s AND name > $d AND id < $n",
            "SELECT 1; SELECT 2",
            "single SELECT",
        ),
    ],
    ids=[
        "unknown-field",
        "bad-param-type",
        "undeclared-placeholder",
        "undeclared-table",
        "positional-placeholder",
        "two-statements",
    ],
)
def test_a_broken_template_refuses_to_load(tmp_path, monkeypatch, old, new, match):
    (tmp_path / "a.yaml").write_text(GOOD.replace(old, new), encoding="utf-8")
    monkeypatch.setenv("VERIFIED_QUERIES_DIR", str(tmp_path))
    vq.reset_vqs()
    with pytest.raises(VQError, match=match):
        vq.vqs()


def test_a_duplicate_name_refuses_to_load(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(GOOD + GOOD.split("verified_queries:")[1], encoding="utf-8")
    monkeypatch.setenv("VERIFIED_QUERIES_DIR", str(tmp_path))
    vq.reset_vqs()
    with pytest.raises(VQError, match="defined twice"):
        vq.vqs()


def test_a_missing_directory_is_an_empty_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIFIED_QUERIES_DIR", str(tmp_path / "nope"))
    vq.reset_vqs()
    assert vq.vqs() == {} and vq.vq_catalog_doc() == ""


# ------------------------------------------------------- visibility
def test_a_vq_over_an_unlisted_table_is_invisible_to_the_role(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch)
    monkeypatch.setattr(governance, "visible_tables", lambda: {"t"})
    assert [v.name for v in vq.visible_vqs()] == ["v1"]
    monkeypatch.setattr(governance, "visible_tables", lambda: {"elsewhere"})
    assert vq.visible_vqs() == []  # one undeclared table hides the template
    assert vq.vq_catalog_doc() == ""  # ...and the prompt section with it


def test_the_catalog_doc_names_queries_questions_and_params(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch)
    doc = vq.vq_catalog_doc()
    assert "call_verified" in doc and "`v1` v1.0" in doc and "d (date)" in doc


# ------------------------------------------------- pipeline (provenance tier)
def test_a_bound_vq_runs_the_pipeline_with_provenance_verified(tmp_path, monkeypatch, local_conn):
    v = _load(tmp_path, monkeypatch)["v1"]
    sql = vq.bind_vq(v, {"d": "2025-02-21", "s": "x1", "n": 10})
    meta = {"name": v.name, "version": v.version, "owner": v.owner, "table": ",".join(v.tables)}
    items = list(pipeline.run(sql, metric=meta, provenance="verified"))
    execute = next(e for e in items if isinstance(e, dict) and e.get("phase") == "execute")
    assert execute["provenance"] == "verified"
    semantic_ev = next(e for e in items if isinstance(e, dict) and e.get("phase") == "semantic")
    assert semantic_ev["metric"] == "v1"  # the reviewed definition is on the trace
