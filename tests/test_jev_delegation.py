"""The study's keep/delegate cost model and the frozen gates."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delegation  # noqa: E402
import usage  # noqa: E402

OPUS = usage.load_prices()["claude-opus-5-5"]


@pytest.mark.parametrize(
    ("start", "k", "add", "out", "keep", "delegate"),
    [
        (500_000, 40, 1_500, 1_000, 5.514, 2.1856),
        (50_000, 3, 3_000, 2_000, 0.2238, 0.5112),
        (300_000, 5, 3_000, 2_000, 0.626, 0.7522),
        (1_000_000, 10, 3_000, 2_000, 2.667, 1.3952),
    ],
)
def test_matches_the_study(start: int, k: int, add: int, out: int, keep: float, delegate: float) -> None:
    assert delegation.keep_cost(k, start, add, out, OPUS) == pytest.approx(keep)
    assert delegation.delegate_cost(k, start, add, out, OPUS, OPUS, 1.0) == pytest.approx(delegate)


def test_long_jobs_in_a_big_session_are_delegated() -> None:
    d = delegation.decide((12, 20, 30, 40, 60), 800_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert d.delegate
    assert d.loss_probability == 0
    assert d.median_calls == 30


def test_short_jobs_are_kept() -> None:
    d = delegation.decide((1, 1, 2, 2, 3), 800_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert not d.delegate
    assert d.expected_saving < 0


def test_a_rare_very_long_job_does_not_carry_the_decision() -> None:
    d = delegation.decide((1,) * 8 + (200, 200), 300_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert d.expected_saving > 0.25
    assert d.expected_saving > 0.15 * d.expected_keep
    assert d.loss_probability == pytest.approx(0.8)
    assert not d.delegate


def test_another_model_is_charged_more_work() -> None:
    same = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, 1.0)
    other = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, delegation.OTHER_MODEL_SCALE)
    assert other > same
