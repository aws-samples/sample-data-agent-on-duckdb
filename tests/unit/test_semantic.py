"""The semantic layer: frozen metric definitions, the metric() surface, and
what happens when the registry does not cover what was asked for."""

import json

import duckdb
import pytest

from data_agent import audit, governance, pipeline, semantic
from data_agent.compute import engine, gate
from data_agent.context import cost, prompt
from data_agent.semantic import compile as sc
from data_agent.semantic import metrics as reg

ADDR = "0x1234567890abcdef1234567890abcdef12345678"
A = {"address": ADDR}


# ------------------------------------------------------------- compilation
def test_definition_is_compiled_from_the_registry_not_from_the_call():
    out = semantic.compile_metric("inbound_eth", params=A, time_window="last_7d")
    sql = out["sql"]
    # the three decisions the number depends on, none of them the model's
    assert "SUM(value) / 1e18" in sql  # wei → ETH
    assert "status = 1" in sql and "call_type = 'call'" in sql  # settled value-moving calls
    assert "FROM eth_traces" in sql  # not eth_transactions
    assert f"lower(to_address) = lower('{ADDR}')" in sql
    # the relative window is resolved to LITERAL dates so the cost layer can
    # expand it into per-day paths (a subquery anchor would not be expandable)
    assert "date BETWEEN '2025-02-18' AND '2025-02-24'" in sql
    assert out["unit"] == "ETH" and out["version"] == "v1"
    assert out["owner"] == "AML investigations"


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("inbound_eth", {"params": A, "time_window": "last_30d"}),
        ("outbound_eth", {"params": A, "time_window": "last_90d"}),
        ("counterparties", {"params": A, "time_window": "last_7d", "group_by": ["date"]}),
        ("sanctioned_inflow", {"params": A, "time_window": "last_7d", "group_by": ["program"]}),
        ("unblended_spend", {"start": "2025-01", "end": "2025-03", "group_by": ["service"]}),
        ("failed_api_calls", {"start": "2025-02-21", "end": "2025-02-22"}),
        ("api_error_rate", {"start": "2025-02-21", "end": "2025-02-23"}),
    ],
)
def test_every_metric_compiles_to_gate_clean_parseable_sql(name, kwargs):
    sql = semantic.compile_metric(name, **kwargs)["sql"]
    assert gate.check(sql) is None
    # DuckDB's own parser accepts it (extract_statements raises on a syntax
    # error, where the read-only gate's single_statement would fall back to True)
    assert len(duckdb.extract_statements(sql)) == 1


def test_group_by_slices_and_orders_and_names_the_dimension():
    out = semantic.compile_metric(
        "inbound_eth", params=A, time_window="last_7d", group_by=["date", "counterparty"]
    )
    assert out["sql"].startswith(
        "SELECT date AS date, from_address AS counterparty, SUM(value) / 1e18 AS inbound_eth"
    )
    assert out["sql"].endswith("GROUP BY date, from_address ORDER BY date, from_address")
    assert out["group_by"] == ["date", "counterparty"]


def test_ratio_metric_guards_the_denominator():
    sql = semantic.compile_metric("api_error_rate", start="2025-02-21", end="2025-02-21")["sql"]
    assert "CAST(COUNT(*) FILTER (WHERE error_code IS NOT NULL) AS DOUBLE) * 100" in sql
    assert "NULLIF(COUNT(*), 0)" in sql  # a day with no events is NULL, not a crash


def test_a_join_metric_qualifies_its_columns():
    sql = semantic.compile_metric(
        "sanctioned_inflow", params=A, time_window="last_7d", group_by=["program"]
    )["sql"]
    assert "JOIN ofac_addresses ON lower(ofac_addresses.address)" in sql
    assert "SUM(eth_traces.value) / 1e18" in sql and "ofac_addresses.program AS program" in sql


def test_explicit_window_is_inclusive_on_both_ends():
    sql = semantic.compile_metric("inbound_eth", params=A, start="2025-02-21", end="2025-02-23")[
        "sql"
    ]
    # BETWEEN, not `>`: `context.cost` expands =, IN, BETWEEN and closed >=/<=
    assert "date BETWEEN '2025-02-21' AND '2025-02-23'" in sql


def test_month_windows_are_resolved_in_months_from_the_env_anchor(monkeypatch):
    sql = semantic.compile_metric("unblended_spend", start="2025-01", end="2025-03")["sql"]
    assert "billing_period BETWEEN '2025-01' AND '2025-03'" in sql
    monkeypatch.setattr(sc, "ANCHOR", "2026-03")
    sql = semantic.compile_metric("unblended_spend", time_window="last_3m")["sql"]
    assert "billing_period BETWEEN '2026-01' AND '2026-03'" in sql
    sql = semantic.compile_metric("unblended_spend", time_window="last_1m")["sql"]
    assert "billing_period BETWEEN '2026-03' AND '2026-03'" in sql
    # one anchor serves both granularities: a monthly metric reads the month
    # that contains the day, so SEMANTIC_ANCHOR needs no per-metric form
    monkeypatch.setattr(sc, "ANCHOR", "2026-03-17")
    sql = semantic.compile_metric("unblended_spend", time_window="last_3m")["sql"]
    assert "billing_period BETWEEN '2026-01' AND '2026-03'" in sql


def test_filters_and_string_values_are_quoted_safely():
    out = semantic.compile_metric(
        "failed_api_calls",
        start="2025-02-21",
        end="2025-02-21",
        filters={"event_name": "O'Brien's call"},
    )
    assert "event_name = 'O''Brien''s call'" in out["sql"]
    assert gate.check(out["sql"]) is None


# --------------------------------------------------------------- not covered
def test_an_undefined_metric_is_refused_and_lists_what_exists():
    with pytest.raises(semantic.MetricNotCovered) as e:
        semantic.compile_metric("gross_margin")
    assert "not defined" in str(e.value) and "inbound_eth" in str(e.value)


@pytest.mark.parametrize(
    ("name", "kwargs", "match"),
    [
        # a dimension the metric does not support, in either position
        (
            "inbound_eth",
            {"params": A, "time_window": "last_7d", "group_by": ["region"]},
            "cannot be sliced by 'region'",
        ),
        (
            "inbound_eth",
            {"params": A, "time_window": "last_7d", "filters": {"region": "x"}},
            "cannot be sliced by 'region'",
        ),
        # a window of the wrong granularity is refused, not converted
        ("inbound_eth", {"params": A, "time_window": "last_3m"}, "partitioned by day"),
        ("unblended_spend", {"time_window": "last_7d"}, "partitioned by month"),
        # parameters
        ("inbound_eth", {"time_window": "last_7d"}, "requires the 'address' parameter"),
        (
            "inbound_eth",
            {"params": {"adress": ADDR}, "time_window": "last_7d"},
            "takes no parameter 'adress'",
        ),
        # windows
        ("inbound_eth", {"params": A}, "may not be scanned without a window"),
        ("inbound_eth", {"params": A, "start": "2025-02-21"}, "needs both start_date and end_date"),
        (
            "inbound_eth",
            {"params": A, "time_window": "last_7d", "start": "2025-02-21", "end": "2025-02-22"},
            "either time_window or start_date/end_date",
        ),
        # a bound that is not a date is refused rather than escaped
        (
            "inbound_eth",
            {"params": A, "start": "yesterday", "end": "today"},
            "must be a YYYY-MM-DD date",
        ),
        (
            "inbound_eth",
            {"params": A, "start": "2025-02", "end": "2025-03"},
            "must be a YYYY-MM-DD date",
        ),
        ("unblended_spend", {"start": "2025-01-01", "end": "2025-03-31"}, "must be a YYYY-MM date"),
    ],
)
def test_a_call_the_registry_does_not_cover_is_refused(name, kwargs, match):
    with pytest.raises(semantic.MetricNotCovered, match=match):
        semantic.compile_metric(name, **kwargs)


def test_a_relative_window_without_an_anchor_is_refused_not_resolved_to_today(monkeypatch):
    """On a static sample dataset "today" reads empty, which looks like an
    answer rather than a missing configuration."""
    monkeypatch.setattr(sc, "ANCHOR", "")
    with pytest.raises(semantic.MetricNotCovered, match="no anchor date"):
        semantic.compile_metric("unblended_spend", time_window="last_3m")


def test_a_parameter_value_that_is_not_a_scalar_is_refused():
    with pytest.raises(semantic.MetricNotCovered, match="strings or numbers"):
        semantic.compile_metric(
            "inbound_eth", time_window="last_7d", params={"address": ["a", "b"]}
        )


# ------------------------------------------------------------- the surface
def test_sql_with_no_metric_call_is_returned_untouched():
    sql = "SELECT   id,name FROM t WHERE id<3"
    out, resolved = semantic.expand(sql)
    assert out == sql and resolved == []  # byte-identical: not re-serialized
    # the word inside an identifier is not a call either
    out, resolved = semantic.expand("SELECT metric_name FROM metrics_audit")
    assert resolved == [] and out == "SELECT metric_name FROM metrics_audit"


def test_unparseable_sql_is_left_for_the_engine_to_report():
    sql = "SELECT * FROM metric("
    out, resolved = semantic.expand(sql)
    assert out == sql and resolved == []


def test_a_call_expands_into_a_subquery_that_composes():
    out, resolved = semantic.expand(
        f"WITH m AS (SELECT * FROM metric('inbound_eth', address => '{ADDR}', "
        "time_window => 'last_7d')) SELECT sum(inbound_eth) AS total FROM m"
    )
    assert len(resolved) == 1 and resolved[0]["metric"] == "inbound_eth"
    assert "SUM(value) / 1e18 AS inbound_eth" in out
    assert "SUM(inbound_eth) AS total" in out and "metric(" not in out.lower()


def test_two_calls_in_one_statement_both_expand_and_keep_their_aliases():
    out, resolved = semantic.expand(
        "SELECT i.inbound_eth - o.outbound_eth AS net FROM "
        f"metric('inbound_eth', address => '{ADDR}', time_window => 'last_7d') AS i, "
        f"metric('outbound_eth', address => '{ADDR}', time_window => 'last_7d') AS o"
    )
    assert [r["metric"] for r in resolved] == ["inbound_eth", "outbound_eth"]
    assert " AS i" in out and " AS o" in out
    assert "LOWER(to_address)" in out and "LOWER(from_address)" in out


def test_the_alias_defaults_to_the_metric_name():
    out, _ = semantic.expand(
        f"SELECT * FROM metric('inbound_eth', address => '{ADDR}', time_window => 'last_7d')"
    )
    assert "AS inbound_eth" in out


def test_named_arguments_accept_both_assignment_forms():
    for arrow in ("=>", ":="):
        _, resolved = semantic.expand(
            f"SELECT * FROM metric('failed_api_calls', start_date {arrow} '2025-02-21', "
            f"end_date {arrow} '2025-02-21', group_by {arrow} 'event_name')"
        )
        assert resolved[0]["group_by"] == ["event_name"]


def test_a_dimension_list_and_a_filter_struct_reach_the_compiler():
    _, resolved = semantic.expand(
        "SELECT * FROM metric('failed_api_calls', start_date => '2025-02-21', "
        "end_date => '2025-02-21', filters => {'event_name': 'ConsoleLogin'}, "
        "group_by => ['principal', 'error_code'])"
    )
    assert "event_name = 'ConsoleLogin'" in resolved[0]["sql"]
    assert resolved[0]["group_by"] == ["principal", "error_code"]


@pytest.mark.parametrize(
    ("sql", "match"),
    [
        ("SELECT * FROM metric()", "takes the metric name first"),
        ("SELECT * FROM metric(inbound_eth)", "takes the metric name first"),
        (f"SELECT * FROM metric('inbound_eth', '{ADDR}')", "must be named"),
        ("SELECT * FROM metric('inbound_eth', time_window => 7)", "must be a quoted string"),
        ("SELECT * FROM metric('failed_api_calls', filters => 'x')", "must be a struct"),
    ],
)
def test_a_malformed_call_is_refused_by_the_surface(sql, match):
    with pytest.raises(semantic.MetricNotCovered, match=match):
        semantic.expand(sql)


# ------------------------------------------------------------- the pipeline
@pytest.fixture()
def traces_conn(monkeypatch):
    """A local `eth_traces` with the columns the on-chain metrics read.

    The cost layer's source list is emptied: path expansion has its own tests,
    what matters here is that the compiled metric SQL reaches the engine.
    """
    conn = duckdb.connect()
    conn.execute(
        """CREATE TABLE eth_traces AS SELECT * FROM (VALUES
             ('0xaaa',    '0xvictim', 1000000000000000000, 1, 'call',         '2025-02-21'),
             ('0xbbb',    '0xvictim', 2000000000000000000, 1, 'call',         '2025-02-22'),
             ('0xccc',    '0xvictim', 5000000000000000000, 0, 'call',         '2025-02-22'),
             ('0xaaa',    '0xvictim', 3000000000000000000, 1, 'delegatecall', '2025-02-23'),
             ('0xvictim', '0xddd',    4000000000000000000, 1, 'call',         '2025-02-23')
           ) AS v(from_address, to_address, value, status, call_type, date)"""
    )
    monkeypatch.setattr(engine, "get_conn", lambda: conn)
    monkeypatch.setattr(cost, "SOURCES", {})
    yield conn
    conn.close()


def _rows(text: str):
    return json.loads(text.split("\n")[0])


def _phases(items):
    return [i["phase"] for i in items if isinstance(i, dict)]


def test_a_metric_call_runs_end_to_end_and_excludes_what_the_definition_excludes(traces_conn):
    items = list(
        pipeline.run(
            "SELECT * FROM metric('inbound_eth', address => '0xVICTIM', "
            "start_date => '2025-02-21', end_date => '2025-02-23')"
        )
    )
    assert _phases(items) == ["gate", "semantic", "rewrite", "execute"]
    # 1 + 2 ETH: the reverted trace and the delegatecall are out of scope, the
    # outbound row is not inbound, and the address matched case-insensitively
    assert _rows(items[-1]) == [{"inbound_eth": "3.0"}]
    assert items[1]["metrics"][0]["metric"] == "inbound_eth"
    assert items[1]["metrics"][0]["version"] == "v1"
    assert items[1]["metrics"][0]["unit"] == "ETH"


def test_the_definition_travels_with_the_result_to_the_model(traces_conn):
    text = pipeline.run_text(
        "SELECT * FROM metric('inbound_eth', address => '0xvictim', "
        "start_date => '2025-02-21', end_date => '2025-02-23')"
    )
    assert "metric inbound_eth v1 — Inbound ETH, in ETH, owned by AML investigations" in text
    assert "rows in scope: status = 1 AND call_type = 'call'" in text
    assert "definition: Value credited to the address" in text
    # ordinary SQL gets no footer
    assert "metric inbound_eth" not in pipeline.run_text("SELECT 1 AS n")


def test_a_not_covered_call_is_rejected_at_the_semantic_stage_and_audited(traces_conn):
    audit.reset_session()
    items = list(pipeline.run("SELECT * FROM metric('gross_margin')"))
    assert _phases(items) == ["gate", "semantic"]
    assert items[1]["rejected"] is True
    assert items[-1].startswith("ERROR: metric 'gross_margin' is not defined")
    assert audit.session_counters()["rejected"] == 1


def test_governance_sees_the_expanded_sql_not_the_metric_name(traces_conn):
    """Order is the point: the RLS predicate lands inside the metric's own
    subquery, so a restricted principal cannot reach rows a metric would
    otherwise have summed."""
    governance.set_active_role("exchange-a:investigator")
    items = list(
        pipeline.run(
            "SELECT * FROM metric('inbound_eth', address => '0xvictim', "
            "start_date => '2025-02-19', end_date => '2025-02-23')"
        )
    )
    rewrite = next(i for i in items if isinstance(i, dict) and i["phase"] == "rewrite")
    assert rewrite["governed"] is True
    assert rewrite["rls"] and "date >= '2025-02-21'" in rewrite["rewritten_sql"]
    assert _rows(items[-1]) == [{"inbound_eth": "3.0"}]  # nothing in scope before the 21st


def test_the_audit_line_records_the_call_and_the_definitions_behind_it(traces_conn, capsys):
    call = (
        "SELECT * FROM metric('inbound_eth', address => '0xvictim', "
        "start_date => '2025-02-21', end_date => '2025-02-23')"
    )
    pipeline.run_text(call)
    rec = json.loads([ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1])
    # the SQL is the request that was made; the metric list is what answered it
    assert rec["audit"]["sql"] == call
    assert rec["audit"]["semantic"] == [{"metric": "inbound_eth", "version": "v1"}]


# ------------------------------------------------------- the prompt catalogue
def test_the_catalogue_lists_only_mounted_metrics_for_an_unrestricted_role():
    doc = semantic.catalog_doc()
    assert "`inbound_eth`" in doc and "`sanctioned_inflow`" in doc
    assert "| `unblended_spend` |" not in doc  # cloud-ops is not mounted by default
    assert "last_7d" in doc and "do not write the aggregate yourself" in doc


def test_the_catalogue_drops_metrics_the_role_may_not_read():
    governance.set_active_role("exchange-a:investigator")
    metrics = semantic.visible_metrics(
        visible=governance.visible_tables(), denied=governance.denied_columns()
    )
    assert "inbound_eth" in metrics  # eth_traces is in the investigator's policy
    assert "failed_api_calls" not in metrics  # cloudtrail_events is not


def test_a_cls_denied_column_is_not_offered_as_a_dimension(monkeypatch):
    from data_agent.storage import registry
    from data_agent.storage.scenarios import cloudops

    monkeypatch.setattr(registry, "enabled", lambda *a: list(registry.SCENARIOS))
    metrics = semantic.visible_metrics(
        visible=frozenset({"cloudtrail_events"}),
        denied={"cloudtrail_events": frozenset({"source_ip_address", "user_agent"})},
    )
    dims = metrics["failed_api_calls"]["dimensions"]
    assert "source_ip" not in dims and "error_code" in dims
    # the registry entry itself is untouched — the filter is per-role
    assert "source_ip" in reg.METRICS["failed_api_calls"]["dimensions"]
    assert cloudops.SCENARIO.name == "cloudops"


def test_the_system_prompt_carries_the_catalogue_and_leaves_no_placeholder():
    text = prompt.build()
    assert "__METRICS__" not in text and "__L0__" not in text
    assert "Defined metrics" in text and "`inbound_eth`" in text
    # a role with no runnable metric gets the section removed, not an empty table
    assert "Defined metrics" not in prompt.build(metrics="")
