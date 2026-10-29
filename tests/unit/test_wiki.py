"""The table wiki and the business glossary are Git assets under
semantic/, loaded fail-closed, and reach the model only through the context
layer with the same governance the cards get: visible tables only, denied
column names masked."""

import pytest

from data_agent import governance
from data_agent.context import datamap, wiki
from data_agent.storage.registry import CARDS


def test_the_shipped_pages_and_terms_load_against_real_cards():
    assert set(wiki.WIKI) <= set(CARDS)
    assert "eth_traces" in wiki.WIKI and "status = 1" in wiki.WIKI["eth_traces"]
    terms = {t["term"]: t for t in wiki.GLOSSARY}
    assert terms["cash-out"]["tables"] == ["eth_traces", "cex_deposit_labels"]
    assert all(set(t["tables"]) <= set(CARDS) for t in wiki.GLOSSARY)


def test_a_search_hit_carries_the_wiki_notes():
    fields = datamap._card_fields("s3t.blockchain.eth_traces", None)
    assert "internal calls" in fields["notes"]  # a catalog-leg name finds the raw page


def test_the_glossary_follows_table_visibility():
    full = wiki.glossary_doc(None)
    assert "**drain**" in full and "**cost**" in full
    onchain = wiki.glossary_doc(frozenset({"eth_traces"}))
    assert "**drain**" in onchain
    assert "**cash-out**" not in onchain  # needs cex_deposit_labels too
    assert "**cost**" not in onchain
    assert wiki.glossary_doc(frozenset()) == ""


def test_the_l0_block_includes_only_the_roles_terms():
    governance.set_active_role("ops:finops")
    entries = [
        {"name": n, "table": n, "database": "main", "kind": "view", "partition": None, "card": c}
        for n, c in CARDS.items()
    ]
    l0 = datamap.l0_doc(entries, governance.visible_tables(), governance.denied_columns())
    assert "**cost**" in l0 and "**drain**" not in l0 and "**AssumeRole count**" not in l0


def _assets(tmp_path, monkeypatch, pages: dict, glossary: str | None = None):
    (tmp_path / "wiki").mkdir()
    for name, text in pages.items():
        (tmp_path / "wiki" / f"{name}.md").write_text(text, encoding="utf-8")
    if glossary is not None:
        (tmp_path / "glossary.yaml").write_text(glossary, encoding="utf-8")
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))


def test_a_denied_column_name_is_masked_in_the_notes(tmp_path, monkeypatch):
    _assets(
        tmp_path, monkeypatch, {"cex_deposit_labels": "join on address; subject_identity is PII"}
    )
    monkeypatch.setattr(wiki, "WIKI", wiki.load_wiki())
    denied = {"cex_deposit_labels": frozenset({"subject_identity"})}
    out = wiki.notes("cex_deposit_labels", denied)
    assert "subject_identity" not in out and "…" in out


@pytest.mark.parametrize(
    ("pages", "glossary", "match"),
    [
        ({"not_a_table": "x"}, None, "no table card"),
        ({"eth_traces": "x" * (wiki.PAGE_MAX + 1)}, None, "chars"),
        ({}, "terms: [{term: t, definition: d, tables: [nope]}]", "unknown tables"),
        ({}, "terms: [{term: t, definition: d, tables: []}]", "must name the tables"),
        ({}, "terms: [{term: t, definition: d, tables: [eth_traces], x: 1}]", "bad entry"),
        (
            {},
            "terms: [{term: t, definition: d, tables: [eth_traces]}, "
            "{term: T, definition: e, tables: [eth_traces]}]",
            "twice",
        ),  # fmt: skip
        ({}, "termz: []", "`terms` list"),
    ],
    ids=["unknown-page", "page-too-long", "unknown-table", "no-tables", "extra-key", "dup", "root"],
)
def test_a_broken_context_asset_refuses_to_load(tmp_path, monkeypatch, pages, glossary, match):
    _assets(tmp_path, monkeypatch, pages, glossary)
    with pytest.raises(wiki.WikiError, match=match):
        wiki.load_wiki()
        wiki.load_glossary()
