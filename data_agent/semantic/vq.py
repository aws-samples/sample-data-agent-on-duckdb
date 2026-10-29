"""Verified queries — reviewed question→SQL templates as Git assets.

A verified query is the Tier-1 semantic asset: a natural-language question
paired with reviewed SQL and typed parameters, stored in
`verified_queries/*.yaml` at the repository root (open format, in the
customer's Git, changed through review + the asset regression gate). A hit
executes the SQL exactly as written — parameters are bound as `exp.Literal`
nodes in the parsed AST (DuckDB's own `$name` placeholder syntax), so a value
can never leave its quotes — and the statement still runs the FULL pipeline:
governance rewrites it for the caller's role, the cost gate prices it, and
provenance lands as `verified` on the execute trace event and the audit line
(the tier reserved for exactly this).

Fail-closed like every other asset: an unparseable file, an unknown field, a
placeholder/params mismatch, an undeclared table or a bad parameter value is
a refusal — the layer never improvises. A missing directory just means "no
verified queries" (the asset is optional; a scenario's metrics are not).

Import shape: like `registry`, this module reads governance for visibility,
so the package facade resolves it lazily through `__getattr__`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from data_agent import governance

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "verified_queries"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")

# Parameter types: how a caller's value is validated before it becomes a bound
# literal. `address` is lowercased (the label tables join on lower(address)).
_SHAPES = {
    "date": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "month": re.compile(r"^\d{4}-\d{2}$"),
    "address": re.compile(r"^0x[0-9a-fA-F]{4,64}$"),
}
PARAM_TYPES = ("date", "month", "address", "string", "int")


class VQError(ValueError):
    """A malformed verified query (operator error, fail-stop at load), or a
    call that does not fit one (caller error, refused per question)."""


@dataclass(frozen=True)
class VerifiedQuery:
    """One reviewed template. `tables` names every governed table the SQL
    reads (visibility and the deny-by-default rewrite key off bare logical
    names); `params` maps placeholder name → type from PARAM_TYPES."""

    name: str
    version: str
    owner: str
    question: str
    sql: str
    tables: tuple[str, ...] = ()
    params: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


_FIELDS = {f.name for f in fields(VerifiedQuery)}


def _parse(sql: str, where: str) -> exp.Expression:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception as e:  # noqa: BLE001 — any parse failure is a bad definition
        raise VQError(f"{where}: sql does not parse ({e})") from e
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise VQError(f"{where}: sql must be a single SELECT")
    return tree


def _placeholders(tree: exp.Expression, where: str) -> set[str]:
    names: set[str] = set()
    for node in tree.find_all(exp.Placeholder, exp.Parameter):
        this = node.this
        if not isinstance(this, str) or not _IDENT_RE.match(this):
            raise VQError(f"{where}: placeholders must be named ($name), not positional")
        names.add(this)
    return names


def validate(v: VerifiedQuery) -> None:
    """Fail-stop on a malformed template (mirrors semantic.model.validate)."""
    where = f"verified query {v.name!r}"
    if not _IDENT_RE.match(v.name):
        raise VQError(f"{where}: name must be a bare identifier")
    if not _VERSION_RE.match(v.version):
        raise VQError(f"{where}: version must look like 1.0 or 1.0.0")
    if not v.owner or not v.question:
        raise VQError(f"{where}: owner and question are required")
    if not v.tables:
        raise VQError(f"{where}: tables must name every table the sql reads")
    for t in v.tables:
        if not _IDENT_RE.match(t):
            raise VQError(f"{where}: table {t!r} must be a bare logical name")
    for p, kind in v.params.items():
        if not _IDENT_RE.match(p):
            raise VQError(f"{where}: parameter {p!r} must be a bare identifier")
        if kind not in PARAM_TYPES:
            raise VQError(f"{where}: parameter {p!r} type {kind!r} not in {PARAM_TYPES}")
    tree = _parse(v.sql, where)
    found = _placeholders(tree, where)
    if found != set(v.params):
        raise VQError(
            f"{where}: sql placeholders {sorted(found)} != declared params {sorted(v.params)}"
        )
    # the catalog must be honest: every real table the sql names is declared,
    # so visibility (and the reader) can trust `tables` — CTE names are local
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    for tbl in tree.find_all(exp.Table):
        name = tbl.name
        if name and name not in ctes and name not in v.tables:
            raise VQError(f"{where}: sql reads {name!r} which is not in tables")


# ------------------------------------------------------------------- loading
def assets_dir() -> Path:
    return Path(os.environ.get("VERIFIED_QUERIES_DIR") or _DEFAULT_DIR)


def load_dir(path: Path) -> dict[str, VerifiedQuery]:
    """Every `*.yaml` under `path` → validated templates, keyed by name.
    A missing directory is an empty catalog; a bad file is a VQError."""
    out: dict[str, VerifiedQuery] = {}
    if not path.is_dir():
        return out
    for file in sorted(path.glob("*.yaml")):
        where = f"verified-query asset {file.name}"
        try:
            doc = yaml.safe_load(file.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise VQError(f"{where}: does not parse ({e})") from e
        if not isinstance(doc, dict) or not isinstance(doc.get("verified_queries"), list):
            raise VQError(f"{where}: must be a mapping with a `verified_queries` list")
        for item in doc["verified_queries"]:
            if not isinstance(item, dict):
                raise VQError(f"{where}: every entry must be a mapping")
            unknown = set(item) - _FIELDS
            if unknown:
                raise VQError(
                    f"{where}: query {item.get('name', '?')!r}: unknown fields {sorted(unknown)}"
                )
            if "tables" in item:
                if not isinstance(item["tables"], list):
                    raise VQError(
                        f"{where}: query {item.get('name', '?')!r}: tables must be a list"
                    )
                item["tables"] = tuple(item["tables"])
            try:
                v = VerifiedQuery(**item)
            except TypeError as e:
                raise VQError(f"{where}: query {item.get('name', '?')!r}: {e}") from e
            validate(v)
            if v.name in out:
                raise VQError(f"{where}: verified query {v.name!r} is defined twice")
            out[v.name] = v
    return out


_cache: dict[str, dict[str, VerifiedQuery]] = {}


def vqs() -> dict[str, VerifiedQuery]:
    if "all" not in _cache:
        _cache["all"] = load_dir(assets_dir())
    return _cache["all"]


def reset_vqs() -> None:
    """Forget the loaded catalog (tests that swap asset directories)."""
    _cache.clear()


def get_vq(name: str) -> VerifiedQuery | None:
    return vqs().get(name)


def visible_vqs() -> list[VerifiedQuery]:
    """Templates whose EVERY table the active persona may read (None = all) —
    the same discipline as metrics: never advertise what the rewrite refuses."""
    vis = governance.visible_tables()
    return [v for v in vqs().values() if vis is None or all(t in vis for t in v.tables)]


# ------------------------------------------------------------------- binding
def _render(kind: str, value, where: str) -> exp.Literal:
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise VQError(f"{where}: must be an integer")
        try:
            return exp.Literal.number(int(value))
        except ValueError as e:
            raise VQError(f"{where}: must be an integer") from e
    if not isinstance(value, str):
        raise VQError(f"{where}: must be a string")
    if kind == "address":
        value = value.lower()
    shape = _SHAPES.get(kind)
    if shape and not shape.match(value):
        raise VQError(f"{where}: {value!r} does not look like a {kind}")
    return exp.Literal.string(value)


def bind_vq(v: VerifiedQuery, params) -> str:
    """The template with every placeholder replaced by a bound literal.

    All declared parameters are required, extras are refused, and each value
    is validated by its declared type before it becomes an AST literal — the
    SQL text itself is never string-interpolated. Models sometimes send the
    params object JSON-encoded; accept that like call_metric's args."""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as e:
            raise VQError(f"params is not valid JSON: {e}") from e
    params = params or {}
    if not isinstance(params, dict):
        raise VQError("params must be an object of {name: value}")
    missing = set(v.params) - set(params)
    extra = set(params) - set(v.params)
    if missing or extra:
        raise VQError(
            f"verified query {v.name!r} takes exactly {sorted(v.params)}"
            + (f"; missing {sorted(missing)}" if missing else "")
            + (f"; unexpected {sorted(extra)}" if extra else "")
        )
    tree = _parse(v.sql, f"verified query {v.name!r}")
    for node in list(tree.find_all(exp.Placeholder, exp.Parameter)):
        name = node.this
        node.replace(_render(v.params[name], params[name], f"parameter {name!r}"))
    return tree.sql(dialect="duckdb")


# -------------------------------------------------------------------- prompt
def vq_catalog_doc(vs: list[VerifiedQuery] | None = None) -> str:
    """The prompt section listing the callable verified queries for this persona."""
    vs = visible_vqs() if vs is None else vs
    if not vs:
        return ""
    lines = [
        "## Verified queries (call_verified) — reviewed question→SQL, run exactly as written",
        "",
        "| Name | Answers | Parameters |",
        "|---|---|---|",
    ]
    for v in vs:
        ps = ", ".join(f"{p} ({k})" for p, k in v.params.items()) or "(none)"
        lines.append(f"| `{v.name}` v{v.version} | {v.question} | {ps} |")
    lines += [
        "",
        "When the user's question matches one of these, call "
        "`call_verified(name, params)` FIRST: the SQL is reviewed and executes exactly as "
        "written (parameters bound as literals), governed like any statement. A name or "
        "parameter that does not fit is refused — report that; for anything else use "
        "call_metric or run_sql.",
    ]
    return "\n".join(lines)
