"""Table wiki + business glossary — customer-owned context assets.

Tier-3 knowledge next to the table cards: what a card cannot hold in four
fields — business rules, the failure cases a team has already paid for, the
words users actually say. Both are open-format files in the customer's Git
under `semantic/` (so the asset regression gate covers them):

    semantic/wiki/<table>.md   free-form notes for one table
    semantic/glossary.yaml     terms → definition + the tables they involve

How they reach the model, through the context layer only:

* wiki pages are indexed with the card text by `search_schema` (in-engine
  fts, zero network) and come back as `notes` on a hit;
* glossary terms ride in the generated L0 block (short by design).

Governance is unchanged and applied here, not trusted to the asset: a page
or a term is shown only when the persona may read the table(s) it is about
(table visibility), and a CLS-denied column name is masked in the text, like
in the cards. Fail-closed at import like every asset: a page whose file name
is not a carded table, a page over the size cap, a term naming an unknown
table, or a malformed glossary raises `WikiError`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from data_agent.storage.registry import CARDS

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "semantic"
PAGE_MAX = 4000  # characters; a page is guidance, not a document dump
NOTES_MAX = 1200  # what one search hit carries


class WikiError(ValueError):
    """A malformed context asset (operator error, fail-stop at import)."""


def _dir() -> Path:
    return Path(os.environ.get("SEMANTIC_ASSETS_DIR") or _DEFAULT_DIR)


def load_wiki(cards=None) -> dict[str, str]:
    cards = CARDS if cards is None else cards
    out: dict[str, str] = {}
    folder = _dir() / "wiki"
    if not folder.is_dir():
        return out
    for page in sorted(folder.glob("*.md")):
        table = page.stem.lower()
        if table not in cards:
            raise WikiError(f"wiki page {page.name}: no table card named {table!r}")
        text = page.read_text(encoding="utf-8").strip()
        if len(text) > PAGE_MAX:
            raise WikiError(f"wiki page {page.name}: {len(text)} chars > {PAGE_MAX}")
        if text:
            out[table] = text
    return out


def load_glossary(cards=None) -> list[dict]:
    cards = CARDS if cards is None else cards
    path = _dir() / "glossary.yaml"
    if not path.exists():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise WikiError(f"glossary.yaml: does not parse ({e})") from e
    terms = (doc or {}).get("terms")
    if not isinstance(terms, list):
        raise WikiError("glossary.yaml: must be a mapping with a `terms` list")
    seen: set[str] = set()
    out = []
    for t in terms:
        if not isinstance(t, dict) or set(t) - {"term", "definition", "tables"}:
            raise WikiError(f"glossary.yaml: bad entry {t!r} (term, definition, tables)")
        term, definition, tables = t.get("term"), t.get("definition"), t.get("tables")
        if not isinstance(term, str) or not term.strip():
            raise WikiError("glossary.yaml: every entry needs a term")
        if not isinstance(definition, str) or not definition.strip():
            raise WikiError(f"glossary.yaml: {term!r} needs a definition")
        if not isinstance(tables, list) or not tables:
            raise WikiError(f"glossary.yaml: {term!r} must name the tables it is about")
        unknown = [x for x in tables if str(x).lower() not in cards]
        if unknown:
            raise WikiError(f"glossary.yaml: {term!r} names unknown tables {unknown}")
        if term.lower() in seen:
            raise WikiError(f"glossary.yaml: {term!r} is defined twice")
        seen.add(term.lower())
        out.append({"term": term, "definition": definition, "tables": [x.lower() for x in tables]})
    return out


WIKI = load_wiki()
GLOSSARY = load_glossary()


def _mask(table: str, text: str, denied: dict[str, frozenset[str]] | None) -> str:
    from .datamap import _scrub_denied  # same masking as the cards

    return _scrub_denied(table, text, denied)


def notes(table: str, denied: dict[str, frozenset[str]] | None = None) -> str | None:
    """The wiki page for a table, masked for the role (caller filters visibility)."""
    text = WIKI.get(table.lower())
    if not text:
        return None
    return _mask(table, text, denied)[:NOTES_MAX]


def glossary_doc(
    visible: frozenset[str] | None = None,
    denied: dict[str, frozenset[str]] | None = None,
) -> str:
    """The prompt section: terms whose every table this role may read."""
    lines = []
    for t in GLOSSARY:
        if visible is not None and not all(x in visible for x in t["tables"]):
            continue
        text = t["definition"]
        for table in t["tables"]:
            text = _mask(table, text, denied)
        lines.append(f"- **{t['term']}** — {text}")
    if not lines:
        return ""
    return "\n".join(["## Business glossary (what users mean by these words)", "", *lines])
