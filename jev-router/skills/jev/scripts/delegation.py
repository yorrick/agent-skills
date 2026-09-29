"""Keep a job in the session, or hand it to a fresh subagent?

The calibration table turns Jev's step score into the call counts really seen in
the study; the cost model (Task 5) prices both options over those counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


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
