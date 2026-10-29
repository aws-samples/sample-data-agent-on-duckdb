"""Metric definitions are Git assets (semantic/<scenario>.yaml), loaded
fail-closed at cold start — a malformed asset refuses to load, exactly like a
malformed governance policy, and the shipped file reproduces the reviewed
definitions field for field."""

import pytest

from data_agent.semantic import assets
from data_agent.semantic.model import MetricError


def test_the_shipped_cloudops_asset_loads_the_six_reviewed_metrics():
    ms = assets.load_scenario_metrics("cloudops")
    assert len(ms) == 6
    top = ms["top_resources"]
    assert top.table == "cur_line_items" and top.kind == "aggregate"
    assert top.fixed_filters == ("line_item_resource_id <> ''",)
    assert ms["mom_delta"].kind == "period_delta" and ms["mom_delta"].dimensions == {}
    assume = ms["assume_role_events_by_principal"]
    assert assume.fixed_filters == ("event_name = 'AssumeRole'",)
    assert assume.dimensions["principal"] == "coalesce(session_issuer_arn, user_arn, invoked_by)"
    assert all(m.version == "1.0" and m.owner and m.description for m in ms.values())


def _write(tmp_path, body: str) -> None:
    (tmp_path / "broken.yaml").write_text(body, encoding="utf-8")


GOOD = """
metrics:
  - name: m1
    version: "1.0"
    owner: o
    description: d
    table: t
    measure_sql: count(*)
    measure_alias: n
    time_column: date
    grain: day
"""


def test_a_good_minimal_asset_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    _write(tmp_path, GOOD)
    assert assets.load_scenario_metrics("broken")["m1"].grain == "day"


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("grain: day", "grain: week", "grain must be month or day"),
        ("measure_sql: count(*)", "measure_sql: (SELECT 1)", "plain expression"),
        ("table: t", "table: t\n    surprise: x", "unknown fields"),
        ("name: m1", "nam: m1", "unknown fields"),
        ("measure_alias: n", "", "missing"),
    ],
    ids=["bad-grain", "subquery-measure", "unknown-field", "typoed-field", "missing-field"],
)
def test_a_broken_field_refuses_to_load(tmp_path, monkeypatch, old, new, match):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    _write(tmp_path, GOOD.replace(old, new))
    with pytest.raises(MetricError, match=match):
        assets.load_scenario_metrics("broken")


def test_a_missing_asset_file_refuses_to_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    with pytest.raises(MetricError, match="not found"):
        assets.load_scenario_metrics("nope")


def test_an_unparseable_document_refuses_to_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    _write(tmp_path, "metrics: [{name: ")
    with pytest.raises(MetricError, match="does not parse"):
        assets.load_scenario_metrics("broken")


def test_a_duplicate_metric_name_refuses_to_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    body = GOOD + GOOD.split("metrics:", 1)[1]
    _write(tmp_path, body)
    with pytest.raises(MetricError, match="defined twice|does not parse|unknown"):
        assets.load_scenario_metrics("broken")


def test_a_non_list_tuple_field_refuses_to_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    _write(tmp_path, GOOD + "    filters: notalist\n")
    with pytest.raises(MetricError, match="must be a list"):
        assets.load_scenario_metrics("broken")
