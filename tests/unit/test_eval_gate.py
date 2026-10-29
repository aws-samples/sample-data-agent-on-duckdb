"""The eval bandwidth gate — per-category pass rates vs a locked
baseline, banded so one case flipping in any current category turns CI red,
while a stale baseline (case counts changed) is a note to refresh, never a
silent re-anchor."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import run_eval  # noqa: E402


def _rows(cat: str, passed: int, total: int) -> list[dict]:
    return [{"category": cat, "passed": i < passed} for i in range(total)]


BASE = {
    "as_of": "2026-09-10 07:05Z",
    "mode": "cloud",
    "categories": {"cost": {"passed": 5, "total": 5}, "ops": {"passed": 13, "total": 13}},
}


def test_bands_narrow_with_category_size():
    assert run_eval._band_pp(52) == 2.0
    assert run_eval._band_pp(13) == 5.0
    assert run_eval._band_pp(5) == 10.0


def test_one_case_regression_breaches_small_and_mid_categories():
    breaches, _ = run_eval.gate_against_baseline(_rows("cost", 4, 5), BASE)
    assert len(breaches) == 1 and breaches[0].startswith("cost: 80% vs baseline 100%")
    breaches, _ = run_eval.gate_against_baseline(_rows("ops", 12, 13), BASE)
    assert len(breaches) == 1 and breaches[0].startswith("ops:")


def test_a_matching_run_and_an_improvement_pass():
    base = {"categories": {"sources": {"passed": 1, "total": 3}}}
    assert run_eval.gate_against_baseline(_rows("sources", 1, 3), base) == ([], [])
    assert run_eval.gate_against_baseline(_rows("sources", 3, 3), base) == ([], [])


def test_a_changed_case_count_is_a_stale_note_not_a_breach():
    breaches, notes = run_eval.gate_against_baseline(_rows("cost", 6, 6), BASE)
    assert breaches == [] and "stale baseline" in notes[0]


def test_a_category_outside_the_baseline_is_a_note():
    breaches, notes = run_eval.gate_against_baseline(_rows("newcat", 2, 2), BASE)
    assert breaches == [] and "not in the baseline" in notes[0]


def test_a_shard_gates_only_the_category_it_ran():
    # the ops shard alone: cost absent from the run is not compared at all
    breaches, notes = run_eval.gate_against_baseline(_rows("ops", 13, 13), BASE)
    assert breaches == [] and notes == []


def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "BASELINE_FILE", tmp_path / "baseline.json")
    run_eval.save_baseline(_rows("cost", 4, 5) + _rows("ops", 13, 13), "cloud")
    base = run_eval.load_baseline()
    assert base["mode"] == "cloud" and base["categories"]["cost"] == {"passed": 4, "total": 5}
    assert run_eval.gate_against_baseline(_rows("cost", 4, 5), base) == ([], [])


def test_the_shipped_baseline_matches_the_published_run():
    """The locked baseline is re-anchored only by `--update-baseline` paired
    with `--golden compute`; it must keep reconciling to a published report.
    Current anchor: docs/eval-results-2026-10-19.md (55/55, in-region
    goldens), replacing the 2026-09-10 seed (50/52)."""
    base = run_eval.load_baseline()
    assert base is not None and base["as_of"].startswith("2026-10-19")
    cats = base["categories"]
    assert sum(c["total"] for c in cats.values()) == 55
    assert sum(c["passed"] for c in cats.values()) == 55
