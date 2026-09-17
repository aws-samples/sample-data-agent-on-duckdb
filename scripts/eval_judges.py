"""Tolerant judges for the eval suite — pure functions, unit-tested.

Golden values are computed at eval time by running `golden_sql` through the
statement pipeline (the same engine, the same S3 data), never copied from a
notebook into the yaml; a judge then decides whether the model's ANSWER TEXT
agrees with the golden rows:

  number   the first cell of the first row, within rel_tol             (default)
  set      every first-column value appears in the answer, any order
  toplist  every first-column value appears, in order (±1 position)
  pair     for every (key, value) row: the key appears and a number within
           rel_tol of the value appears in the answer

The text extractor is deliberately simple (`_NUM_RE` for numbers, case-
insensitive substring for labels): it is what a reviewer reading the answer
would do, and it is exactly reproducible.
"""

from __future__ import annotations

import re

_NUM_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])-?\d+(?:\.\d+)?")
JUDGES = ("number", "set", "toplist", "pair")


def numbers(text: str) -> list[float]:
    vals = []
    for m in _NUM_RE.findall(text):
        try:
            vals.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return vals


def _close(a: float, b: float, rel_tol: float) -> bool:
    return abs(a - b) <= rel_tol * abs(b) if b else abs(a) <= rel_tol


def _as_float(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def judge_number(answer: str, rows: list[tuple], rel_tol: float = 0.01) -> tuple[bool, str]:
    if not rows or not rows[0]:
        return False, "golden query returned no rows"
    target = _as_float(rows[0][0])
    if target is None:
        return False, f"golden value {rows[0][0]!r} is not numeric"
    got = numbers(answer)
    if any(_close(v, target, rel_tol) for v in got):
        return True, f"number {target:g} found (±{rel_tol:.0%})"
    return False, f"no number within {rel_tol:.0%} of {target:g} in answer (saw {got[:8]})"


def _labels(rows: list[tuple]) -> list[str]:
    return [str(r[0]) for r in rows if r and r[0] is not None and str(r[0]).strip()]


def judge_set(answer: str, rows: list[tuple], rel_tol: float = 0.01) -> tuple[bool, str]:
    labels = _labels(rows)
    if not labels:
        return False, "golden query returned no labels"
    low = answer.lower()
    missing = [x for x in labels if x.lower() not in low]
    if missing:
        return False, f"answer lacks {missing[:5]} of the golden set ({len(labels)})"
    return True, f"all {len(labels)} golden labels present"


def judge_toplist(
    answer: str, rows: list[tuple], rel_tol: float = 0.01, slack: int = 1
) -> tuple[bool, str]:
    """Ordered top-N: every label present, and each label's rank in the answer
    (order of first occurrence) within `slack` of its golden rank."""
    labels = _labels(rows)
    if not labels:
        return False, "golden query returned no labels"
    low = answer.lower()
    positions = {x: low.find(x.lower()) for x in labels}
    missing = [x for x, p in positions.items() if p < 0]
    if missing:
        return False, f"answer lacks {missing[:5]} of the golden top-{len(labels)}"
    order = sorted(labels, key=positions.get)
    off = [(x, i, order.index(x)) for i, x in enumerate(labels) if abs(order.index(x) - i) > slack]
    if off:
        x, want, got = off[0]
        return False, f"{x!r} is #{got + 1} in the answer, golden #{want + 1} (slack {slack})"
    return True, f"top-{len(labels)} order agrees (±{slack})"


def judge_pair(answer: str, rows: list[tuple], rel_tol: float = 0.01) -> tuple[bool, str]:
    if not rows or any(len(r) < 2 for r in rows):
        return False, "golden query must return (key, value) rows"
    low = answer.lower()
    got = numbers(answer)
    for key, value in ((str(r[0]), _as_float(r[1])) for r in rows):
        if key.lower() not in low:
            return False, f"answer lacks key {key!r}"
        if value is None:
            return False, f"golden value for {key!r} is not numeric"
        if not any(_close(v, value, rel_tol) for v in got):
            return False, f"no number within {rel_tol:.0%} of {value:g} for {key!r}"
    return True, f"all {len(rows)} (key, value) pairs agree"


def judge(kind: str, answer: str, rows: list[tuple], rel_tol: float = 0.01) -> tuple[bool, str]:
    fn = {
        "number": judge_number,
        "set": judge_set,
        "toplist": judge_toplist,
        "pair": judge_pair,
    }.get(kind)
    if fn is None:
        return False, f"unknown judge {kind!r} (one of {JUDGES})"
    return fn(answer, rows, rel_tol)
