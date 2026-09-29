"""Keep a job in the session, or hand it to a fresh subagent?

The calibration table turns Jev's step score into the call counts really seen in
the study; the cost model (Task 5) prices both options over those counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from usage import Prices


@dataclass(frozen=True)
class Bin:
    max_score: float | None  # None: every score above the previous bin
    calls: tuple[int, ...]


def load_calibration(path: Path | None = None) -> tuple[Bin, ...]:
    data = json.loads((path or Path(__file__).with_name("calibration.json")).read_text())
    return tuple(Bin(max_score=b["max_score"], calls=tuple(b["calls"])) for b in data["bins"])


def calls_for(bins: tuple[Bin, ...], score: float) -> tuple[int, ...]:
    for b in bins:
        if b.max_score is None or score <= b.max_score:
            return b.calls
    return bins[-1].calls


# The study's cost model (docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md),
# applied to a uniform job built from the session's recent averages. Keep: call i
# reads start + i*add from cache, writes `add` new tokens to the 1-hour cache and
# produces `out`. Delegate: a brief (the parent's next call, writing a brief
# instead of working), the helper's calls on a context that starts at 25k tokens
# and grows the same way, and a relay (one parent call that caches the brief and
# the helper's answer and produces a short reply).
HELPER_START = 25_000
BRIEF_OUT = 2_000
ANSWER = 2_000
RELAY_OUT = 800
# Reason: a helper on a different model is assumed to need 25% more calls, as the
# study assumed for Sonnet 5; the margins below absorb the rest.
OTHER_MODEL_SCALE = 1.25
# The user's frozen gates (decision D3).
MIN_SAVING_USD = 0.25
MIN_SAVING_SHARE = 0.15
MAX_LOSS_PROBABILITY = 0.20


def keep_cost(k: int, start: int, add: int, out: int, p: Prices) -> float:
    reads = k * start + add * k * (k - 1) // 2
    return reads * p.read + k * add * p.w1h + k * out * p.out


def delegate_cost(k: int, start: int, add: int, out: int, parent: Prices, helper: Prices, scale: float) -> float:
    brief = start * parent.read + add * parent.w1h + BRIEF_OUT * parent.out
    # Call 0 writes HELPER_START; call i >= 1 reads HELPER_START + (i-1)*add and writes `add`.
    helper_reads = (k - 1) * HELPER_START + add * (k - 2) * (k - 1) // 2
    helper_writes = HELPER_START + (k - 1) * add
    work = helper_reads * helper.read + helper_writes * helper.w1h + k * out * helper.out
    relay = (start + add) * parent.read + (BRIEF_OUT + ANSWER) * parent.w1h + RELAY_OUT * parent.out
    return brief + scale * work + relay


@dataclass(frozen=True)
class Decision:
    delegate: bool
    expected_keep: float
    expected_saving: float
    loss_probability: float
    median_calls: int


def decide(
    calls: tuple[int, ...], start: int, add: int, out: int, parent: Prices, helper: Prices, same_model: bool
) -> Decision:
    """Price both options over every call count the calibration saw for this score."""
    scale = 1.0 if same_model else OTHER_MODEL_SCALE
    keeps = [keep_cost(k, start, add, out, parent) for k in calls]
    savings = [
        keep - delegate_cost(k, start, add, out, parent, helper, scale) for k, keep in zip(calls, keeps, strict=True)
    ]
    expected_keep = sum(keeps) / len(keeps)
    expected_saving = sum(savings) / len(savings)
    loss = sum(s < 0 for s in savings) / len(savings)
    # Reason: the loss gate stops a rare very long job from making the average
    # positive while most jobs with this score would lose money.
    delegate = (
        expected_saving >= MIN_SAVING_USD
        and expected_saving >= MIN_SAVING_SHARE * expected_keep
        and loss <= MAX_LOSS_PROBABILITY
    )
    return Decision(delegate, expected_keep, expected_saving, loss, sorted(calls)[len(calls) // 2])
