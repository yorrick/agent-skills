#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Fit jev-router's calibration table from the study's labelled turns.

Usage: uv run scripts/fit_jev_calibration.py ~/work/data/jev-router-study/study/jev_steps.jsonl

Each labelled turn has Jev's step score, asked with the agent's previous reply
(the "context" answer), and the number of model calls the turn really took. Even
rows fit five equal-count bins of the score; odd rows are held out to check them.
The table keeps every observed call count, so the router can price the long tail
as it really is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BINS = 5
OUT = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts" / "calibration.json"


def pairs_from(path: Path) -> list[tuple[float, int]]:
    pairs = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        answer = row.get("context")
        if isinstance(answer, dict) and isinstance(answer.get("score"), int | float) and row.get("n_calls"):
            pairs.append((float(answer["score"]), int(row["n_calls"])))
    return pairs


def fit(pairs: list[tuple[float, int]], bins: int = BINS) -> list[dict]:
    ordered = sorted(pairs)
    size = len(ordered) / bins
    table = []
    for b in range(bins):
        chunk = ordered[round(b * size) : round((b + 1) * size)]
        table.append({"max_score": chunk[-1][0] if b < bins - 1 else None, "calls": sorted(n for _, n in chunk)})
    return table


def check(table: list[dict], pairs: list[tuple[float, int]]) -> list[str]:
    lines = []
    for i, b in enumerate(table):
        low = table[i - 1]["max_score"] if i else None
        high = b["max_score"]
        held = [n for s, n in pairs if (low is None or s > low) and (high is None or s <= high)]
        predicted = sum(n >= 11 for n in b["calls"]) / len(b["calls"])
        seen = sum(n >= 11 for n in held) / len(held) if held else 0.0
        where = f"score <= {high}" if high is not None else f"score > {low}"
        lines.append(f"bin {i} ({where}): predicted 11+ {predicted:.0%}, held-out {seen:.0%} of {len(held)}")
    return lines


def main(argv: list[str]) -> int:
    pairs = pairs_from(Path(argv[0]).expanduser())
    table = fit(pairs[0::2])
    for line in check(table, pairs[1::2]):
        print(line)
    OUT.write_text(json.dumps({"harness": "claude", "fitted_on": len(pairs[0::2]), "bins": table}, indent=1) + "\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
