"""Table cards + three-way reconciliation (doc ↔ card ↔ engine), offline.

The cards are the reviewed middle between what the prompt says, what the
policies govern and what the engine really exposes. These tests run with zero
network: the "engine" is a local DuckDB with tables built FROM the cards, so
the mechanics — including the negative cases — are exercised; the live check
against S3 is `scripts/reconcile.py` (same functions, real engine).
"""

import dataclasses

import duckdb
import pytest

from data_agent.context import datamap, reconcile
from data_agent.storage import registry
from data_agent.storage.scenarios import cloudops, onchain
from data_agent.storage.scenarios.base import Scenario, TableCard

ACCT = "123456789012"
BUCKET = f"s3://duckdb-agent-ducklake-{ACCT}"


def _onchain() -> Scenario:  # every optional dataset on: labels mounted
    return onchain.build(f"{BUCKET}/labels")


def _cloudops() -> Scenario:  # curated + native CUR + CloudTrail macro
    return cloudops.build(f"{BUCKET}/ops", "s3://cur/x", "s3://trail/AWSLogs/1/CloudTrail", ACCT)


def _engine_from_cards(sc: Scenario, overrides: dict[str, list[str]] | None = None):
    """A local DuckDB whose tables carry exactly the cards' columns (or an
    override), standing in for the real engine's DESCRIBE."""
    conn = duckdb.connect()
    for name in sc.views:
        cols = (overrides or {}).get(name) or list(sc.cards[name].columns)
        conn.execute(f"CREATE TABLE {name} AS SELECT {', '.join(f'1 AS {c}' for c in cols)}")

    def columns(table: str) -> set[str]:
        return {r[0] for r in conn.execute(f"DESCRIBE {table}").fetchall()}

    return columns


# ------------------------------------------------------------- coverage
@pytest.mark.parametrize("sc", [_onchain(), _cloudops()], ids=["onchain", "cloudops"])
def test_every_mounted_table_has_a_card(sc):
    assert set(sc.views) <= set(sc.cards), set(sc.views) - set(sc.cards)
    for card in sc.cards.values():
        assert card.grain and card.use_when and card.columns and card.owner


@pytest.mark.parametrize("sc", [_onchain(), _cloudops()], ids=["onchain", "cloudops"])
def test_every_table_is_reachable_by_a_persona_or_declared_admin_only(sc):
    named = reconcile.personas_naming(sc.policies)
    for table, card in sc.cards.items():
        if card.admin_only:
            assert table not in named, f"{table} is admin_only yet a persona lists it"
        else:
            assert table in named, f"{table}: no restricted persona lists it"


def test_cw_logs_is_admin_only_by_omission():
    # The runtime's own log export is never reachable by a restricted role
    assert cloudops.CARDS["cw_logs"].admin_only
    assert not any("cw_logs" in p["tables"] for p in cloudops.build_policies(ACCT).values())


def test_registry_merges_cards_by_bare_name_for_every_access_path():
    cards = registry.cards()
    assert set(cards) == set(onchain.CARDS) | set(cloudops.CARDS)
    assert registry.CARDS["eth_traces"] is onchain.CARDS["eth_traces"]
    # a catalog-leg copy of the same logical table resolves to the raw card
    assert datamap._bare("s3t.blockchain.eth_traces") == "eth_traces"


# ------------------------------------------------------------- doc ↔ card
def test_cloudops_doc_hand_written_columns_all_reconcile():
    sc = _cloudops()
    cols = reconcile.doc_columns(sc.doc, set(sc.views))
    assert set(cols) == {"cur_line_items", "cloudtrail_events", "cw_logs", "cur_raw"}
    assert len(cols["cur_line_items"]) >= 11 and len(cols["cloudtrail_events"]) >= 19
    for table, names in cols.items():
        assert names <= {c.lower() for c in sc.cards[table].columns}, (table, names)


def test_onchain_doc_columns_all_reconcile():
    sc = _onchain()
    cols = reconcile.doc_columns(sc.doc, set(sc.views))
    assert cols["eth_traces"] >= {"from_address", "to_address", "call_type", "trace_type"}
    assert cols["cex_deposit_labels"] == {
        "address",
        "exchange",
        "label",
        "subject_identity",
        "risk_tier",
    }
    for table, names in cols.items():
        assert names <= {c.lower() for c in sc.cards[table].columns}, (table, names)


def test_doc_parser_takes_list_cells_whole_and_prose_cells_conservatively():
    doc = (
        "| Table (view) | Partition | What it is | Key columns |\n|---|---|---|---|\n"
        "| t | `day` | one row per thing, curated Parquet (also `region` column) | a_b, c, d_e |\n"
        "| u | `day` | prose mentioning big_col and words | x |\n"
    )
    cols = reconcile.doc_columns(doc, {"t", "u"})
    # partition cell + backticked aside count; prose cell contributes backticked names only
    assert cols["t"] == {"day", "region", "a_b", "c", "d_e"}
    assert cols["u"] == {"day", "big_col", "x"}  # prose: underscore identifiers; list cell: all
    notes_only = (
        "| Table | Path | Notes |\n|---|---|---|\n| t | s3://b/t_x/ | uses foo_bar; `baz` |\n"
    )
    assert reconcile.doc_columns(notes_only, {"t"})["t"] == {"foo_bar", "baz"}  # path skipped


def test_a_wrong_column_name_in_the_doc_is_a_drift():
    sc = _cloudops()
    bad_doc = sc.doc.replace("line_item_unblended_cost", "line_item_unblendd_cost")
    broken = dataclasses.replace(sc, doc=bad_doc)
    kinds = {(d.kind, d.table) for d in reconcile.check_scenario(broken)}
    assert ("doc_column", "cur_line_items") in kinds
    assert not reconcile.check_scenario(sc)  # the real doc is clean


# ---------------------------------------------------------- policy ↔ card
def test_policy_row_filters_and_denied_columns_exist_on_the_cards():
    for sc in (_onchain(), _cloudops()):
        for table, cols in reconcile.policy_columns(sc.policies).items():
            assert cols <= {c.lower() for c in sc.cards[table].columns}, (table, cols)
    cols = reconcile.policy_columns(_cloudops().policies)
    assert cols["cloudtrail_events"] == {"recipient_account_id", "source_ip_address", "user_agent"}
    assert cols["cur_line_items"] == {"line_item_usage_account_id", "line_item_resource_id"}


def test_a_denied_column_that_no_longer_exists_is_a_drift():
    sc = _cloudops()
    pol = {k: dict(v) for k, v in sc.policies.items()}
    pol["ops:secops-junior"] = {
        **pol["ops:secops-junior"],
        "tables": {
            "cloudtrail_events": {
                "row_filter": f"recipient_account_id = '{ACCT}'",
                "deny_columns": ["source_ip_addr"],  # typo: governs nothing
            }
        },
    }
    drifts = reconcile.check_scenario(dataclasses.replace(sc, policies=pol))
    assert any(d.kind == "policy_column" and "source_ip_addr" in d.detail for d in drifts)


# ---------------------------------------------------------- card ↔ engine
@pytest.mark.parametrize("sc", [_onchain(), _cloudops()], ids=["onchain", "cloudops"])
def test_cards_agree_with_an_engine_built_from_them(sc):
    assert reconcile.check_scenario(sc, _engine_from_cards(sc)) == []


def test_card_claiming_a_column_the_engine_lacks_is_a_drift():
    sc = _onchain()
    cols = [c for c in sc.cards["eth_traces"].columns if c != "trace_id"]
    drifts = reconcile.check_scenario(sc, _engine_from_cards(sc, {"eth_traces": cols}))
    assert [(d.kind, d.table) for d in drifts] == [("phantom_column", "eth_traces")]
    assert "trace_id" in drifts[0].detail


def test_engine_column_missing_from_a_complete_card_is_a_drift_but_not_from_a_partial_one():
    sc = _cloudops()
    extra = {
        "cloudtrail_events": [*sc.cards["cloudtrail_events"].columns, "new_col"],  # complete
        "cur_line_items": [*sc.cards["cur_line_items"].columns, "product_sku"],  # partial card
    }
    drifts = reconcile.check_scenario(sc, _engine_from_cards(sc, extra))
    assert [(d.kind, d.table) for d in drifts] == [("undocumented_column", "cloudtrail_events")]


def test_unreachable_table_is_reported_not_raised():
    sc = _onchain()

    def boom(table):
        raise RuntimeError("HTTP 403")

    drifts = reconcile.check_scenario(sc, boom)
    assert drifts and all(d.kind == "phantom_column" and "403" in d.detail for d in drifts)


def test_missing_card_and_missing_persona_are_drifts():
    sc = Scenario(
        name="x",
        views={"a": "read_parquet('s3://b/a.parquet')", "b": "read_parquet('s3://b/b.parquet')"},
        cards={"b": TableCard(grain="g", use_when="u", columns=("x",), owner="o")},
        policies={"x:r": {"allow_raw_paths": False, "tables": {}}},
    )
    kinds = sorted((d.kind, d.table) for d in reconcile.check_scenario(sc))
    assert kinds == [("no_card", "a"), ("no_persona", "b")]


# ------------------------------------------------------------ the model view
def test_l0_carries_the_card_summary_and_search_ranks_on_card_text(local_conn):
    local_conn.execute("CREATE VIEW ofac_addresses AS SELECT '0xa' AS address, 'CYBER' AS program")
    local_conn.execute(
        "CREATE VIEW cex_deposit_labels AS SELECT 'exchange-a' AS exchange, "
        "'c1' AS subject_identity, '0xa' AS address, 'hot' AS risk_tier"
    )
    entries = datamap.catalog_entries(local_conn)
    doc = datamap.l0_doc(entries)
    assert (
        "| `ofac_addresses` | view | — (catalog/label table, no filter needed) | one row = one OFAC"
        in doc
    )
    assert "| Grain |" in doc
    import threading

    hits = datamap.search(local_conn, threading.Lock(), "sanctions screening", limit=3)
    assert hits[0]["table"] == "ofac_addresses"
    assert hits[0]["use_when"].startswith("sanctions screening") and hits[0]["caveats"]


def test_card_text_never_names_a_denied_column_for_a_restricted_role(local_conn):
    local_conn.execute(
        "CREATE VIEW cex_deposit_labels AS SELECT 'exchange-a' AS exchange, "
        "'c1' AS subject_identity, '0xa' AS address, 'hot' AS risk_tier"
    )
    denied = {"cex_deposit_labels": frozenset({"subject_identity"})}
    import threading

    hits = datamap.search(
        local_conn,
        threading.Lock(),
        "cash-out deposit",
        limit=3,
        visible=frozenset({"cex_deposit_labels"}),
        denied=denied,
    )
    assert hits[0]["table"] == "cex_deposit_labels"
    blob = " ".join(str(v) for v in hits[0].values())
    assert "subject_identity" not in blob and "…" in hits[0]["caveats"]
    doc = datamap.l0_doc(datamap.catalog_entries(local_conn), denied=denied)
    assert "subject_identity" not in doc
