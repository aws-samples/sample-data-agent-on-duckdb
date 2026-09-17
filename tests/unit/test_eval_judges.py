"""Eval judges — pure functions over (answer text, golden rows)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import eval_judges as j  # noqa: E402

ANSWER = (
    "Total unblended cost for 2026-08 was **$540.80**. Top services: AmazonEC2 (305.59), "
    "AmazonRDS (55.44), AmazonEKS (47.14), AmazonVPC (43.93), AWSELB (27.41). "
    "1,034,397 line items."
)


def test_number_judge_extracts_thousands_separators_and_applies_rel_tol():
    ok, note = j.judge_number(ANSWER, [(540.7,)], rel_tol=0.01)
    assert ok and "540.7" in note
    assert j.judge_number(ANSWER, [(1034397,)], rel_tol=0.001)[0]
    ok, note = j.judge_number(ANSWER, [(600,)], rel_tol=0.01)
    assert not ok and "no number within 1%" in note


def test_number_judge_rejects_empty_or_non_numeric_golden():
    assert j.judge_number(ANSWER, [])[0] is False
    assert j.judge_number(ANSWER, [("AmazonEC2",)])[0] is False


def test_number_judge_zero_target_uses_absolute_tolerance():
    assert j.judge_number("there were 0 events", [(0,)])[0]
    assert not j.judge_number("there were 3 events", [(0,)])[0]


def test_set_judge_is_order_insensitive_and_case_insensitive():
    rows = [("amazonrds",), ("AmazonEC2",), ("AWSELB",)]
    assert j.judge_set(ANSWER, rows)[0]
    ok, note = j.judge_set(ANSWER, [("AmazonS3",), ("AmazonEC2",)])
    assert not ok and "AmazonS3" in note


def test_toplist_judge_accepts_exact_order_and_one_position_slack():
    golden = [("AmazonEC2",), ("AmazonRDS",), ("AmazonEKS",), ("AmazonVPC",), ("AWSELB",)]
    assert j.judge_toplist(ANSWER, golden)[0]
    swapped = "Top: AmazonEC2, AmazonEKS, AmazonRDS, AmazonVPC, AWSELB"  # #2/#3 swapped: ok
    assert j.judge_toplist(swapped, golden)[0]
    far = "Top: AWSELB, AmazonEC2, AmazonRDS, AmazonEKS, AmazonVPC"  # AWSELB #5 → #1: not ok
    ok, note = j.judge_toplist(far, golden)
    assert not ok and "AWSELB" in note and "#1" in note


def test_toplist_judge_reports_missing_labels_before_order():
    ok, note = j.judge_toplist("Top: AmazonEC2 only", [("AmazonEC2",), ("AmazonRDS",)])
    assert not ok and "lacks" in note and "AmazonRDS" in note


def test_pair_judge_needs_every_key_with_a_close_number():
    rows = [("AmazonEC2", 305.59), ("AmazonRDS", 55.44)]
    assert j.judge_pair(ANSWER, rows)[0]
    ok, note = j.judge_pair(ANSWER, [("AmazonEC2", 999.0)])
    assert not ok and "999" in note
    ok, note = j.judge_pair(ANSWER, [("AmazonS3", 1.0)])
    assert not ok and "AmazonS3" in note
    assert not j.judge_pair(ANSWER, [("AmazonEC2",)])[0]  # single-column rows are not pairs


def test_dispatcher_names_unknown_judges_and_routes_known_ones():
    ok, note = j.judge("median", ANSWER, [(1,)])
    assert not ok and "unknown judge" in note
    assert j.judge("number", ANSWER, [(540.8,)])[0]
    assert j.judge("set", ANSWER, [("AmazonEKS",)])[0]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("costs 1,234.5 USD and 7 items", [1234.5, 7.0]),
        ("vol-0123456789abcdef0 at 26.12", [26.12]),  # ids are not numbers
        ("2026-08 had 540.80", [540.8]),  # dates are not numbers (dash-joined)
    ],
)
def test_number_extractor(text, expected):
    got = j.numbers(text)
    assert all(any(abs(g - e) < 1e-9 for g in got) for e in expected), (got, expected)
