"""Table cards are Git assets (semantic/cards/<scenario>.yaml), loaded
fail-closed like the metric definitions: a malformed card refuses to load,
and the shipped files are what every reconcile check and the data map read."""

import pytest

from data_agent.storage import card_assets, registry
from data_agent.storage.card_assets import CardError


def test_the_shipped_cards_are_the_ones_the_registry_serves():
    onchain = card_assets.load_cards("onchain")
    cloudops = card_assets.load_cards("cloudops")
    assert set(onchain) | set(cloudops) == set(registry.CARDS)
    traces = onchain["eth_traces"]
    assert traces.grain.startswith("one row") and "date" in traces.columns
    assert cloudops["cw_logs"].admin_only is True  # admin-only by omission
    assert cloudops["cur_line_items"].complete is False  # documented subset of 116


GOOD = """
cards:
  t:
    grain: one row = one thing
    use_when: always
    columns: [a, b]
"""


def _load(tmp_path, monkeypatch, body):
    (tmp_path / "cards").mkdir(exist_ok=True)
    (tmp_path / "cards" / "x.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    return card_assets.load_cards("x")


def test_a_minimal_card_loads_with_defaults(tmp_path, monkeypatch):
    c = _load(tmp_path, monkeypatch, GOOD)["t"]
    assert c.columns == ("a", "b") and c.complete is True and c.admin_only is False


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("    use_when: always\n", "", "use_when"),
        ("columns: [a, b]", "columns: a", "list of names"),
        ("columns: [a, b]", "columns: [a, a]", "twice"),
        ("use_when: always", "use_when: always\n    surprise: 1", "unknown fields"),
        ("use_when: always", "use_when: always\n    complete: maybe", "true or false"),
        ("cards:", "kards:", "`cards` mapping"),
    ],
    ids=["no-use-when", "columns-not-list", "dup-column", "unknown-field", "bad-bool", "bad-root"],
)
def test_a_broken_card_refuses_to_load(tmp_path, monkeypatch, old, new, match):
    with pytest.raises(CardError, match=match):
        _load(tmp_path, monkeypatch, GOOD.replace(old, new))


def test_a_missing_card_file_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("SEMANTIC_ASSETS_DIR", str(tmp_path))
    with pytest.raises(CardError, match="not found"):
        card_assets.load_cards("nope")
