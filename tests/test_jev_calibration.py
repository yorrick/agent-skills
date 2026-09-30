"""Jev's step score mapped to the call counts really seen in the study."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delegation  # noqa: E402

_spec = importlib.util.spec_from_file_location("fit_jev_calibration", REPO / "scripts" / "fit_jev_calibration.py")
assert _spec and _spec.loader
fit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_mod)


def test_fit_makes_equal_count_bins_ordered_by_score() -> None:
    pairs = [(i / 10, i) for i in range(50)]
    table = fit_mod.fit(pairs, bins=5)
    assert [len(b["calls"]) for b in table] == [10] * 5
    assert [b["max_score"] for b in table] == [0.9, 1.9, 2.9, 3.9, None]
    assert table[0]["calls"] == list(range(10))


def test_calls_for_picks_the_first_bin_that_covers_the_score(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"bins": [{"max_score": 1.0, "calls": [1, 2]}, {"max_score": None, "calls": [30]}]}))
    bins = delegation.load_calibration(path)
    assert delegation.calls_for(bins, 0.4) == (1, 2)
    assert delegation.calls_for(bins, 1.0) == (1, 2)
    assert delegation.calls_for(bins, 3.9) == (30,)


def test_check_compares_long_job_shares_on_held_out_rows() -> None:
    table = [{"max_score": 1.0, "calls": [1, 1, 20, 20]}, {"max_score": None, "calls": [20]}]
    lines = fit_mod.check(table, [(0.5, 1), (0.5, 30), (2.0, 40)])
    assert lines[0] == "bin 0 (score <= 1.0): predicted 11+ 50%, held-out 50% of 2"
    assert lines[1] == "bin 1 (score > 1.0): predicted 11+ 100%, held-out 100% of 1"


def test_shipped_table_covers_every_score() -> None:
    bins = delegation.load_calibration()
    assert bins[-1].max_score is None
    assert all(b.calls for b in bins)
    assert delegation.calls_for(bins, 0.0) and delegation.calls_for(bins, 4.0)
