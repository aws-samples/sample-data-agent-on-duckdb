"""Semantic layer v0: metric registry, deterministic compiler, the
Semantic stage, `call_metric`, and — because parameter compilation is a new
AuthZ-adjacent surface — the adversarial cases: injection through dims /
filters / time_window, unregistered metrics and dimensions, governance on the
compiled SQL (RLS applied, CLS-denied column refused, table-level deny)."""

import asyncio
import dataclasses
import json

import pytest

import agent as agent_mod
from data_agent import audit, governance, pipeline, semantic
from data_agent.compute import engine
from data_agent.context import cost, prompt, results
from data_agent.semantic import model
from data_agent.storage.scenarios import cloudops, onchain

# the default policies pin RLS to OPS_ACCOUNT_ID; unset in the unit suite → 'unset-account'
# (fail closed), so the seeded rows carry exactly that value to be visible
ACCT = cloudops.account_id()
M = cloudops.METRICS
CHAIN = onchain.METRICS
COST = M["unblended_cost_by_service"]
ASSUME = M["assume_role_events_by_principal"]
IN_ETH = CHAIN["inbound_eth"]
OUT_ETH = CHAIN["outbound_eth"]


def _semantic_ops_engine(local_conn, monkeypatch):
    """Local stand-ins for the ops tables (columns from the cards), rows for
    two months / one day, cost sources cleared so no S3 path expansion."""
    monkeypatch.setattr(cost, "SOURCES", {})
    local_conn.execute(
        "CREATE VIEW cur_line_items AS SELECT * FROM (VALUES "
        f"('2026-07', '{ACCT}', 'AmazonEC2', 'us-west-2', 'BoxUsage', 'Usage', 'vol-1', 100.0), "
        f"('2026-08', '{ACCT}', 'AmazonEC2', 'us-west-2', 'BoxUsage', 'Usage', 'vol-1', 300.0), "
        f"('2026-08', '{ACCT}', 'AmazonRDS', 'us-west-2', 'InstanceUsage', 'Usage', 'db-1', 50.0), "
        f"('2026-08', '999999999999', 'AmazonS3', 'us-east-1', 'Requests', 'Usage', 'b', 999.0)"
        ") t(billing_period, line_item_usage_account_id, line_item_product_code, "
        "product_region_code, line_item_usage_type, line_item_line_item_type, "
        "line_item_resource_id, line_item_unblended_cost)"
    )
    local_conn.execute(
        "CREATE VIEW cloudtrail_events AS SELECT * FROM (VALUES "
        f"('2026-09-02', '{ACCT}', 'AssumeRole', 'arn:aws:iam::1:role/a', NULL, NULL, 'us-west-2', NULL, 'AssumedRole'), "  # noqa: E501
        f"('2026-09-02', '{ACCT}', 'AssumeRole', 'arn:aws:iam::1:role/a', NULL, NULL, 'us-west-2', NULL, 'AssumedRole'), "  # noqa: E501
        f"('2026-09-02', '{ACCT}', 'GetObject', 'arn:aws:iam::1:role/a', NULL, NULL, 'us-west-2', NULL, 'AssumedRole')"  # noqa: E501
        ") t(date, recipient_account_id, event_name, session_issuer_arn, user_arn, invoked_by, "
        "aws_region, error_code, user_type)"
    )
    local_conn.execute("CREATE VIEW cw_logs AS SELECT '2026-09-02' AS date, 's1' AS log_stream")
    governance.set_schema_prober(engine.probe_columns)


def _run(sql, metric=None):
    return list(pipeline.run(sql, metric=metric))


def _call(**kw):
    """Drive the call_metric async tool synchronously; return (events, text)."""

    async def go():
        items = []
        async for item in agent_mod.call_metric._tool_func(**kw):
            items.append(item)
        return items

    items = asyncio.run(go())
    return [i for i in items if isinstance(i, dict)], items[-1]


# ------------------------------------------------------------------ registry
def test_registry_loads_every_scenario_metric_and_validates_it():
    ms = semantic.metrics()
    assert set(ms) == set(M) | set(CHAIN) and all(isinstance(m, model.Metric) for m in ms.values())
    assert semantic.get("mom_delta").kind == "period_delta"
    assert semantic.get("nope") is None


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"name": "bad name"}, "bare identifier"),
        ({"version": "v1"}, "version"),
        ({"grain": "week"}, "grain"),
        ({"measure_sql": "sum(x) FROM t"}, "does not parse"),
        ({"measure_sql": "(SELECT 1)"}, "plain expression"),
        ({"dimensions": {"svc": "line_item_product_code", "x y": "z"}}, "bare identifier"),
        ({"default_dimensions": ("region_x",)}, "not a dimension"),
        ({"fixed_filters": ("1; DROP TABLE t",)}, "WHERE predicate"),
        ({"table": "s3t.blockchain.eth_traces"}, "bare identifier"),
    ],
)
def test_malformed_metric_definitions_fail_stop(bad, msg):
    with pytest.raises(model.MetricError, match=msg):
        model.validate(dataclasses.replace(COST, **bad))


def test_visible_metrics_follow_the_persona_table_visibility():
    assert {m.name for m in semantic.visible_metrics()} == set(M) | set(CHAIN)  # admin: all
    governance.set_active_role("ops:finops")
    names = {m.name for m in semantic.visible_metrics()}
    assert "unblended_cost_by_service" in names and "top_resources" in names
    assert "assume_role_events_by_principal" not in names  # cloudtrail not in finops policy
    assert "runtime_log_events_by_stream" not in names  # cw_logs is admin-only
    governance.set_active_role("ops:secops")
    assert {m.name for m in semantic.visible_metrics()} == {"assume_role_events_by_principal"}
    # the exchange personas read eth_traces, so they see the ledger metrics and
    # none of the cloud-ops ones (the cross-scenario isolation, on the catalog)
    governance.set_active_role("exchange-a:junior")
    assert {m.name for m in semantic.visible_metrics()} == set(CHAIN)
    assert "inbound_eth" in semantic.catalog_doc() and "unblended" not in semantic.catalog_doc()


def test_catalog_doc_lists_only_visible_metrics_and_rides_in_the_l0_block(local_conn):
    governance.set_active_role("ops:finops")
    doc = semantic.catalog_doc()
    assert "`unblended_cost_by_service` v1.0" in doc and "call_metric" in doc
    assert "assume_role_events_by_principal" not in doc
    l0 = prompt.l0_block()
    assert "Registered metrics" in l0 and "mom_delta" in l0
    assert prompt.build(l0).count("Registered metrics") == 1


def test_metric_columns_reconcile_with_the_table_cards():
    """Every column a metric uses exists on its table's card (table-card discipline)."""
    import re

    from data_agent.storage.registry import CARDS

    for m in semantic.metrics().values():
        cols = {c.lower() for c in CARDS[m.table].columns}
        used = set()
        for expr in [m.measure_sql, *m.dimensions.values(), *m.fixed_filters]:
            used |= {t for t in re.findall(r"[a-z_][a-z0-9_]*", expr.lower()) if "_" in t}
        used |= set(m.filters) | {m.time_column}
        used -= {"round", "sum", "count", "coalesce"}
        assert used <= cols, (m.name, used - cols)


# ------------------------------------------------------------------ compiler
def test_compile_is_deterministic_and_bounds_the_time_column():
    a = semantic.compile(COST, ["service"], "2026-08", None, 5)
    b = semantic.compile(COST, ["service"], {"eq": "2026-08"}, None, 5)
    assert a == b == semantic.compile(COST, ["service"], ["2026-08"], None, 5)
    assert "billing_period = '2026-08'" in a and "GROUP BY 1" in a and "LIMIT 5" in a
    rng = semantic.compile(COST, None, {"from": "2026-07", "to": "2026-08"}, None, 20)
    assert "BETWEEN '2026-07' AND '2026-08'" in rng and "line_item_product_code AS service" in rng
    lst = semantic.compile(COST, ["service", "region"], ["2026-07", "2026-08"], None, 20)
    assert "IN ('2026-07', '2026-08')" in lst and "GROUP BY 1, 2" in lst


def test_compiled_sql_satisfies_the_cost_gate_by_construction():
    sql = semantic.compile(COST, ["service"], "2026-08", None, 5)
    monkey_sources = dict(cloudops.build_sources("s3://duckdb-agent-ducklake-1/ops", ""))
    saved = dict(cost.SOURCES)
    cost.SOURCES.clear()
    cost.SOURCES.update(monkey_sources)
    try:
        out, info = cost.apply(sql)
        assert info["partition_filter"] and info["expanded_paths"] == 1
        assert "billing_period=2026-08/*.parquet" in out
    finally:
        cost.SOURCES.clear()
        cost.SOURCES.update(saved)


def test_time_window_is_required_and_shape_checked():
    with pytest.raises(semantic.SemanticError, match="time_window is required"):
        semantic.compile(COST, None, None)
    with pytest.raises(semantic.SemanticError, match="YYYY-MM"):
        semantic.compile(COST, None, "2026-08-01")  # day literal on a month metric
    with pytest.raises(semantic.SemanticError, match="YYYY-MM-DD"):
        semantic.compile(ASSUME, None, "2026-09")
    with pytest.raises(semantic.SemanticError, match="from > to"):
        semantic.compile(COST, None, {"from": "2026-09", "to": "2026-07"})
    with pytest.raises(semantic.SemanticError, match="YYYY-MM"):
        semantic.compile(COST, None, "2026-08' OR '1'='1")  # never reaches the SQL


@pytest.mark.parametrize(
    "dims",
    [
        ["line_item_product_code' OR 1=1 --"],
        ["service; DROP TABLE cur_line_items"],
        ["service", "subject_identity"],
        ["*"],
        [42],
        "service, region",
    ],
)
def test_unregistered_or_hostile_dimensions_are_refused_before_any_sql(dims):
    with pytest.raises(semantic.SemanticError) as e:
        semantic.compile(COST, dims, "2026-08")
    assert "OR 1=1" not in str(e.value) and "DROP" not in str(e.value)  # never echoed as SQL


def test_filter_values_become_bound_literals_never_sql():
    hostile = "Usage' OR 1=1 --"
    sql = semantic.compile(COST, ["service"], "2026-08", {"line_item_line_item_type": hostile})
    assert "line_item_line_item_type = 'Usage'' OR 1=1 --'" in sql  # one quoted literal
    assert " OR 1=1" not in sql.replace("'Usage'' OR 1=1 --'", "")
    sql = semantic.compile(
        COST, ["service"], "2026-08", {"line_item_product_code": ["AmazonEC2", "AmazonRDS"]}
    )
    assert "line_item_product_code IN ('AmazonEC2', 'AmazonRDS')" in sql


def test_unregistered_filter_columns_and_bad_limits_are_refused():
    with pytest.raises(semantic.SemanticError, match="filter column not registered"):
        semantic.compile(COST, None, "2026-08", {"line_item_resource_id": "vol-1"})
    with pytest.raises(semantic.SemanticError, match="filters must be an object"):
        semantic.compile(COST, None, "2026-08", ["x"])
    with pytest.raises(semantic.SemanticError, match="between 1 and"):
        semantic.compile(COST, None, "2026-08", None, 0)
    with pytest.raises(semantic.SemanticError, match="between 1 and"):
        semantic.compile(COST, None, "2026-08", None, semantic.MAX_LIMIT + 1)
    with pytest.raises(semantic.SemanticError, match="integer"):
        semantic.compile(COST, None, "2026-08", None, "5; DROP")


def test_fixed_filters_and_period_delta_shape():
    sql = semantic.compile(ASSUME, ["principal"], "2026-09-02")
    assert "event_name = 'AssumeRole'" in sql
    assert "COALESCE(session_issuer_arn, user_arn, invoked_by) AS principal" in sql
    sql = semantic.compile(M["mom_delta"], None, {"from": "2026-07", "to": "2026-08"})
    assert "LAG(unblended_cost_usd) OVER (ORDER BY billing_period)" in sql
    assert "AS delta" in sql and "AS pct_change" in sql and "GROUP BY 1" in sql


# ----------------------------------------------------- pipeline + governance
def test_semantic_stage_passes_plain_sql_through_untouched(local_conn):
    items = _run("SELECT id FROM t WHERE id < 3")
    assert [i["phase"] for i in items if isinstance(i, dict)] == ["gate", "rewrite", "execute"]
    assert "semantic" not in [type(s).__name__.lower() for s in []]  # no semantic event emitted
    assert items[-1].startswith("[")


def test_semantic_stage_emits_the_metric_event_and_the_audit_line_carries_it(
    local_conn, monkeypatch, capsys
):
    _semantic_ops_engine(local_conn, monkeypatch)
    sql = semantic.compile(COST, ["service"], "2026-08")
    meta = {"name": COST.name, "version": COST.version, "owner": COST.owner, "table": COST.table}
    audit.reset_session()
    items = _run(sql, metric=meta)
    phases = [i["phase"] for i in items if isinstance(i, dict)]
    assert phases == ["gate", "semantic", "rewrite", "execute"]
    sem = items[1]
    assert sem["metric"] == "unblended_cost_by_service" and sem["version"] == "1.0"
    rows = json.loads(items[-1].split("\n")[0])
    assert results.unmark(rows[0]["service"]) == "AmazonS3"  # admin: every account
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    assert json.loads(line)["audit"]["metric"] == "unblended_cost_by_service@1.0"


def test_compiled_metric_is_governed_finops_sees_only_its_account(local_conn, monkeypatch):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:finops")
    events, text = _call(
        metric="unblended_cost_by_service", dims=["service"], time_window="2026-08"
    )
    rewrite = next(e for e in events if e["phase"] == "rewrite")
    assert rewrite["governed"] and rewrite["rls"][0]["table"] == "cur_line_items"
    rows = json.loads(text.split("\n")[0])
    assert [results.unmark(r["service"]) for r in rows] == ["AmazonEC2", "AmazonRDS"]
    assert rows[0]["unblended_cost_usd"] == "300.0"


def test_finops_calling_top_resources_is_refused_by_column_policy(local_conn, monkeypatch):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:finops")
    events, text = _call(metric="top_resources", time_window="2026-08")
    rewrite = next(e for e in events if e["phase"] == "rewrite")
    assert rewrite["rejected"] and "line_item_resource_id" in rewrite["reason"]
    assert text.startswith("ERROR: governance") and "vol-1" not in text


def test_secops_junior_calling_the_cw_logs_metric_is_refused_at_table_level(
    local_conn, monkeypatch
):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:secops-junior")
    events, text = _call(metric="runtime_log_events_by_stream", time_window="2026-09-02")
    # not visible to the role → refused as unregistered, without revealing it exists
    assert events[0]["phase"] == "semantic" and events[0]["rejected"]
    assert text.startswith("OUT_OF_SCOPE") and "cw_logs" not in text
    assert "assume_role_events_by_principal" in text  # what the role CAN call


def test_secops_assume_role_metric_runs_governed_and_returns_rows(local_conn, monkeypatch):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:secops")
    events, text = _call(
        metric="assume_role_events_by_principal", dims=["principal"], time_window="2026-09-02"
    )
    assert [e["phase"] for e in events] == ["gate", "semantic", "rewrite", "execute"]
    rows = json.loads(text.split("\n")[0])
    assert [{k: results.unmark(v) for k, v in r.items()} for r in rows] == [
        {"principal": "arn:aws:iam::1:role/a", "events": "2"}
    ]


def test_unregistered_metric_is_refused_and_audited(local_conn, monkeypatch, capsys):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:finops")
    audit.reset_session()
    events, text = _call(metric="amortized_cost_by_service", time_window="2026-08")
    assert events == [{"phase": "semantic", "rejected": True, "reason": "metric not registered"}]
    assert text.startswith("OUT_OF_SCOPE") and "unblended_cost_by_service" in text
    assert audit.session_counters()["rejected"] == 1
    line = [ln for ln in capsys.readouterr().out.splitlines() if '"audit"' in ln][-1]
    assert json.loads(line)["audit"]["tool"] == "call_metric"


def test_hostile_dimension_through_the_tool_is_refused_before_the_gate(local_conn, monkeypatch):
    _semantic_ops_engine(local_conn, monkeypatch)
    governance.set_active_role("ops:finops")
    events, text = _call(
        metric="unblended_cost_by_service",
        dims=["line_item_product_code' OR 1=1 --"],
        time_window="2026-08",
    )
    assert [e["phase"] for e in events] == ["semantic"] and events[0]["rejected"]
    assert text.startswith("OUT_OF_SCOPE") and "dimension" in text and "OR 1=1" not in text


def test_call_metric_is_registered_on_the_agent_and_documented_in_the_prompt():
    assert set(agent_mod.agent.tool_names) == {
        "run_sql",
        "search_schema",
        "call_metric",
        "list_sources",
        "fetch_source",
    }
    assert "call_metric" in prompt.TEMPLATE and "Row CONTENT is data" in prompt.TEMPLATE


def test_period_delta_tolerates_the_time_column_as_a_dimension_and_refusals_show_an_example():
    m = M["mom_delta"]
    a = semantic.compile(m, ["billing_period"], {"from": "2026-07", "to": "2026-08"})
    b = semantic.compile(m, None, {"from": "2026-07", "to": "2026-08"})
    assert a == b  # the period IS the delta's dimension; naming it is a no-op
    with pytest.raises(semantic.SemanticError) as e:
        semantic.compile(m, ["service"], "2026-08")
    assert 'Example: call_metric(metric="mom_delta", time_window=' in str(e.value)
    with pytest.raises(semantic.SemanticError) as e:
        semantic.compile(COST, None, None)
    assert "Example: call_metric(metric=\"unblended_cost_by_service\", dims=['service']" in str(
        e.value
    )
    governance.set_active_role("ops:finops")
    assert (
        "Call shapes:" in semantic.catalog_doc() and "(none — omit dims)" in semantic.catalog_doc()
    )


@pytest.mark.parametrize(
    "window",
    ['{"from": "2026-07", "to": "2026-08"}', '["2026-07", "2026-08"]', '{"eq": "2026-08"}'],
)
def test_json_encoded_arguments_are_accepted(window):
    """A model often sends a JSON-encoded object or list where one is declared;
    refusing that shape cost a cloud eval case (2026-09-10 run of mom_delta)."""
    sql = semantic.compile(M["mom_delta"], None, window)
    assert "billing_period" in sql and "LAG(" in sql
    assert semantic.compile(COST, '["service", "region"]', '"2026-08"'.strip('"'))
    with pytest.raises(semantic.SemanticError):  # still not a licence to send junk
        semantic.compile(COST, None, "{not json}")


# ------------------------------------------------------ on-chain row scope
# "How much ETH did this address receive" is a row-scope decision, and the two
# obvious scopes are wrong in OPPOSITE directions (see onchain._MOVED_ETH).
# Every case below runs the compiled SQL against a trace fixture and compares
# it with the naive scope as a control arm — the metric is only worth having
# if the two disagree on rows a real ledger contains.
EXPLOITER = "0x47666fab8bd0ac7003bce3f5c3585383f09486e2"
VICTIM = "0x1111111111111111111111111111111111111111"
BENEFICIARY = "0x2222222222222222222222222222222222222222"

# (from, to, value_wei, status, call_type, trace_type)   1e18 wei = 1 ETH
TRACES = [
    (VICTIM, EXPLOITER, 5 * 10**18, 1, "call", "call"),  # real: drained
    # a delegatecall frame inherits the parent's value (EIP-7): the trace
    # reports 5 ETH again while no ether moves. `value > 0 AND status = 1`
    # counts it a second time.
    (VICTIM, EXPLOITER, 5 * 10**18, 1, "delegatecall", "call"),
    (VICTIM, EXPLOITER, 5 * 10**18, 1, "staticcall", "call"),  # same, read-only frame
    (VICTIM, EXPLOITER, 9 * 10**18, 0, "call", "call"),  # reverted: no ether moved
    # real movements with a NULL call_type — `call_type = 'call'` drops them
    (EXPLOITER, BENEFICIARY, 2 * 10**18, 1, None, "create"),  # contract endowment
    (EXPLOITER, BENEFICIARY, 1 * 10**18, 1, None, "suicide"),  # selfdestruct payout
    # a block reward: no sender, real ETH received. In scope by design — the
    # `trace_type` dimension is what separates mining income from transfers.
    (None, EXPLOITER, 3 * 10**18, 1, None, "reward"),
]


def _traces_engine(local_conn, monkeypatch, rows=TRACES, date="2025-02-21", extra=()):
    """A local stand-in for `eth_traces` (columns from the card), cost sources
    cleared so nothing expands to S3. `rows` all land on `date`; `extra` rows
    carry their own date as a 7th field (for the governance window)."""
    monkeypatch.setattr(cost, "SOURCES", {})
    values = ", ".join(
        "("
        + ", ".join(
            [
                "NULL" if f is None else f"'{f}'",
                f"'{t}'",
                str(v),
                str(status),
                "NULL" if call_type is None else f"'{call_type}'",
                f"'{trace_type}'",
                f"'{row_date}'",
            ]
        )
        + ")"
        for f, t, v, status, call_type, trace_type, row_date in (
            [(*r, date) for r in rows] + list(extra)
        )
    )
    local_conn.execute(
        f"CREATE OR REPLACE VIEW eth_traces AS SELECT * FROM (VALUES {values}) "  # nosec B608 - test fixture
        "t(from_address, to_address, value, status, call_type, trace_type, date)"
    )
    governance.set_schema_prober(engine.probe_columns)


def _scalar(conn, sql, column):
    rows = conn.execute(sql).fetchall()
    cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=True))[column] for r in rows]


def test_onchain_metrics_compile_the_reviewed_row_scope():
    for m in CHAIN.values():
        sql = semantic.compile(m, None, "2025-02-21", {"to_address": EXPLOITER})
        assert "status = 1" in sql and "value > 0" in sql
        assert "call_type IS NULL OR call_type = 'call'" in sql
        assert "date = '2025-02-21'" in sql  # window required → cost gate satisfied


def test_delegatecall_frames_are_not_double_counted(local_conn, monkeypatch):
    """Control arm: the naive scope reports 15 ETH transferred in, the metric 5."""
    _traces_engine(local_conn, monkeypatch)
    scope = {"to_address": EXPLOITER, "trace_type": "call"}
    sql = semantic.compile(IN_ETH, ["recipient"], "2025-02-21", scope)
    assert _scalar(local_conn, sql, "eth_in") == [5.0]
    naive = (
        "SELECT round(sum(value) / 1e18, 6) AS eth_in FROM eth_traces "
        f"WHERE date = '2025-02-21' AND to_address = '{EXPLOITER}' AND trace_type = 'call' "
        "AND value > 0 AND status = 1"
    )  # nosec B608 - control arm, literals are test constants
    assert _scalar(local_conn, naive, "eth_in") == [15.0]  # 3× the same 5 ETH
    # and the reward is still there when the trace_type filter is dropped
    everything = semantic.compile(IN_ETH, ["recipient"], "2025-02-21", {"to_address": EXPLOITER})
    assert _scalar(local_conn, everything, "eth_in") == [8.0]  # 5 transferred + 3 mined


def test_create_suicide_and_reward_rows_are_not_dropped(local_conn, monkeypatch):
    """The other direction: `call_type = 'call'` would see none of these."""
    _traces_engine(local_conn, monkeypatch)
    sql = semantic.compile(OUT_ETH, ["counterparty"], "2025-02-21", {"from_address": EXPLOITER})
    assert _scalar(local_conn, sql, "eth_out") == [3.0]  # create 2 + suicide 1
    by_type = semantic.compile(OUT_ETH, ["trace_type"], "2025-02-21", {"from_address": EXPLOITER})
    assert sorted(_scalar(local_conn, by_type, "trace_type")) == ["create", "suicide"]
    dropped = (
        "SELECT count(*) AS n FROM eth_traces WHERE date = '2025-02-21' "
        f"AND from_address = '{EXPLOITER}' AND value > 0 AND status = 1 AND call_type = 'call'"
    )  # nosec B608 - control arm, literals are test constants
    assert _scalar(local_conn, dropped, "n") == [0]  # every real outflow lost


def test_a_checksummed_address_still_matches_the_lower_case_ledger(local_conn, monkeypatch):
    """An address pasted in EIP-55 form must not silently return zero rows —
    an empty result reads like an answer."""
    _traces_engine(local_conn, monkeypatch)
    mixed = "0x47666FaB8bd0Ac7003bcE3f5C3585383F09486E2"
    sql = semantic.compile(IN_ETH, ["recipient"], "2025-02-21", {"to_address": mixed})
    assert f"LOWER(to_address) = LOWER('{mixed}')" in sql  # value still a bound literal
    assert _scalar(local_conn, sql, "eth_in") == [8.0]
    exact = sql.replace(f"LOWER(to_address) = LOWER('{mixed}')", f"to_address = '{mixed}'")
    assert local_conn.execute(exact).fetchall() == []  # control arm: the silent zero
    lst = semantic.compile(IN_ETH, None, "2025-02-21", {"to_address": [mixed, VICTIM]})
    assert "LOWER(to_address) IN (LOWER(" in lst


def test_case_insensitive_filters_must_be_declared_filters():
    with pytest.raises(model.MetricError, match="not a filter column"):
        model.validate(dataclasses.replace(IN_ETH, case_insensitive_filters=("block_hash",)))


def test_fanout_and_drain_curve_shapes(local_conn, monkeypatch):
    _traces_engine(local_conn, monkeypatch)
    fanout = semantic.compile(
        CHAIN["counterparty_fanout"], None, "2025-02-21", {"from_address": EXPLOITER}
    )
    assert "COUNT(DISTINCT to_address) AS recipients" in fanout
    assert _scalar(local_conn, fanout, "recipients") == [1]  # BENEFICIARY, counted once
    curve = semantic.compile(
        CHAIN["eth_outflow_daily"], None, {"from": "2025-02-21", "to": "2025-02-23"}
    )
    assert "LAG(eth_out) OVER (ORDER BY date)" in curve and "AS pct_change" in curve


def test_the_ledger_metric_is_governed_by_the_investigator_row_filter(local_conn, monkeypatch):
    """The compiled metric is not privileged: the persona's RLS (`eth_traces`
    from 2025-02-21 on) still bounds it — a wider window does not widen it."""
    # the extra row is the day BEFORE the persona's cutoff, inside the window
    before = (VICTIM, EXPLOITER, 100 * 10**18, 1, "call", "call", "2025-02-20")
    _traces_engine(local_conn, monkeypatch, extra=[before])
    window = {"from": "2025-02-20", "to": "2025-02-21"}
    kw = dict(metric="inbound_eth", dims=["recipient"], filters={"to_address": EXPLOITER})
    _, text = _call(**kw, time_window=window)  # admin sees both days
    assert [results.unmark(r["eth_in"]) for r in json.loads(text.split("\n")[0])] == ["108.0"]
    governance.set_active_role("exchange-a:investigator")
    events, text = _call(**kw, time_window=window)
    rewrite = next(e for e in events if e["phase"] == "rewrite")
    assert rewrite["governed"] and rewrite["rls"][0]["table"] == "eth_traces"
    rows = json.loads(text.split("\n")[0])
    assert [results.unmark(r["eth_in"]) for r in rows] == ["8.0"]  # the 2025-02-20 row is out


def test_the_eth_traces_card_states_both_wrong_scopes(local_conn):
    """The card is what a run_sql user reads; the metric's scope must not be the
    only place the finding lives (the L0 map carries the card's caveats)."""
    card = onchain.CARDS["eth_traces"]
    assert "DOUBLE-COUNTS" in card.caveats and "UNDER-COUNTS" in card.caveats
    assert "delegatecall" in card.caveats and "call_type IS NULL" in card.caveats
