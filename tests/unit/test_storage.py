"""Storage layer: the scenario contract and the registry that merges scenarios."""

from data_agent.context import cost, datamap
from data_agent.storage import legs, registry
from data_agent.storage.scenarios import cloudops, onchain
from data_agent.storage.scenarios.base import Scenario


def test_registry_lists_both_scenarios_and_only_enabled_ones_mount():
    assert [s.name for s in registry.SCENARIOS] == ["onchain", "cloudops"]
    assert onchain.SCENARIO.enabled  # raw views are always on
    assert not cloudops.SCENARIO.enabled  # no OPS_* env in the unit suite
    assert [s.name for s in registry.enabled()] == ["onchain"]
    assert set(registry.view_defs()) == set(onchain.VIEW_DEFS)


def test_partitioned_sources_are_what_the_cost_gate_and_data_map_read():
    assert set(registry.PARTITIONED_SOURCES) == set(onchain.PARTITIONED_SOURCES)
    assert registry.PARTITIONED_SOURCES is datamap.PARTITIONED_SOURCES
    assert cost.SOURCES == registry.PARTITIONED_SOURCES  # a copy, tests monkeypatch it
    for spec in registry.PARTITIONED_SOURCES.values():
        assert {"column", "granularity", "base", "layout", "reader", "args"} <= set(spec)


def test_default_policies_come_from_every_scenario_mounted_or_not():
    p = registry.default_policies()
    assert {"exchange-a:investigator", "exchange-a:junior"} <= set(p)  # onchain
    assert {"ops:finops", "ops:secops", "ops:secops-junior"} <= set(p)  # cloudops, not mounted
    assert "ops:admin" not in p  # the platform role belongs to governance, not a scenario


def test_catalog_doc_is_assembled_from_enabled_scenarios_then_active_legs():
    ops = cloudops.build("s3://duckdb-agent-ducklake-123456789012/ops", "", "", "")
    doc = registry.catalog_doc(["dl"], scenarios=[onchain.SCENARIO, ops])
    assert doc.index("AWS Public Blockchain dataset") < doc.index("Cloud-ops data")
    assert doc.index("Cloud-ops data") < doc.index("`dl` — DuckLake")
    assert "S3 Tables native" not in doc  # inactive leg never leaks
    assert registry.catalog_doc([], scenarios=[]) == ""


def test_a_new_scenario_is_one_object_the_registry_merges(monkeypatch):
    extra = Scenario(
        name="hr",
        views={"payroll": "read_parquet('s3://b/payroll/*/*.parquet', hive_partitioning=1)"},
        sources={
            "payroll": {
                "column": "month",
                "granularity": "month",
                "base": "s3://b/payroll",
                "layout": "{base}/month={part}/*.parquet",
                "reader": "read_parquet",
                "args": "hive_partitioning=1",
            }
        },
        doc="\n## Payroll\n",
        policies={"hr:analyst": {"allow_raw_paths": False, "tables": {"payroll": {}}}},
    )
    monkeypatch.setattr(registry, "SCENARIOS", (onchain.SCENARIO, extra))
    assert "payroll" in registry.view_defs()
    assert registry.partitioned_sources()["payroll"]["column"] == "month"
    assert "hr:analyst" in registry.default_policies()
    assert "## Payroll" in registry.catalog_doc([])


def test_legs_doc_section_names_only_active_legs():
    assert legs.doc_section([]) == ""
    doc = legs.doc_section(["s3t", "glue"])
    assert "`s3t` — S3 Tables" in doc and "`glue` — same Iceberg tables" in doc
    assert "DuckLake" not in doc
    assert "| `s3t.*` |" in doc and "| `dl.*` |" not in doc
