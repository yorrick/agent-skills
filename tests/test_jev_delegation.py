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


@pytest.mark.parametrize(
    ("keep", "delegate", "passes"),
    [
        # Each list is one cost per call count in the calibration sample.
        ((1.0,) * 5, (0.75,) * 5, True),  # saving exactly $0.25 (25% of keep)
        ((1.0,) * 5, (0.7501,) * 5, False),  # just under $0.25
        ((20.0,) * 5, (17.0,) * 5, True),  # saving exactly 15% of keep ($3.00)
        ((20.0,) * 5, (17.01,) * 5, False),  # just under 15%
        ((20.0,) * 5, (10.0, 10.0, 10.0, 10.0, 20.5), True),  # loss probability exactly 20%
        ((20.0,) * 5, (10.0, 10.0, 10.0, 20.5, 20.5), False),  # 40%
    ],
)
def test_the_gates_hold_at_their_exact_boundaries(
    monkeypatch: pytest.MonkeyPatch, keep: tuple[float, ...], delegate: tuple[float, ...], passes: bool
) -> None:
    """Decision D3's gates are inclusive: saving >= $0.25, saving >= 15% of the
    expected keep cost, loss probability <= 20%. The two cost functions are
    replaced by exact figures, keyed by the call count they are asked about."""
    calls = (1, 2, 3, 4, 5)
    monkeypatch.setattr(delegation, "keep_cost", lambda k, *rest: keep[k - 1])
    monkeypatch.setattr(delegation, "delegate_cost", lambda k, *rest: delegate[k - 1])
    d = delegation.decide(calls, 800_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert d.delegate is passes


def test_another_model_is_charged_more_work() -> None:
    same = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, 1.0)
    other = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, delegation.OTHER_MODEL_SCALE)
    assert other > same
