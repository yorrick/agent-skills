"""Reports: shadow mode's decisions against what really happened, and the fork check's verdict."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from pathlib import Path

import delegation
import usage


def _when(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def turn_after(entries: list[dict], sha: str, near: str | None = None) -> list[dict] | None:
    """From the typed message with this hash to the next typed message. With `near`,
    the occurrence closest in time to it, so a repeated "continue" finds its own turn."""
    typed_at = [i for i, e in enumerate(entries) if usage.is_typed(e)]
    matches = [i for i in typed_at if usage.prompt_sha(usage.entry_text(entries[i]) or "") == sha]
    if not matches:
        return None
    start = matches[0]
    target = _when(near) if near else None
    if target is not None:

        def distance(i: int) -> float:
            when = _when(str(entries[i].get("timestamp", "")))
            return abs((when - target).total_seconds()) if when else float("inf")

        start = min(matches, key=distance)
    end = next((i for i in typed_at if i > start), len(entries))
    return entries[start:end]


def period_events(events: list[dict], points: list[dict]) -> list[dict]:
    """Every cache-aware hook event from the first scored job's capture to the last
    one's, plus a minute: the last hook logs its event just after its snapshot."""
    if not points:
        return []
    first = datetime.fromisoformat(points[0]["meta"]["created"])
    last = datetime.fromisoformat(points[-1]["meta"]["created"]) + timedelta(seconds=60)
    return [e for e in events if e.get("version") == 3 and (t := _when(str(e.get("ts", "")))) and first <= t <= last]


def shadow_rows(events: list[dict], prices: dict) -> list[dict]:
    rows = []
    cache: dict[str, list[dict]] = {}
    for e in events:
        if e.get("version") != 3 or e.get("outcome") not in ("keep", "delegate"):
            continue
        path = e["transcript_path"]
        if path not in cache:
            cache[path] = usage.read_entries(Path(path)) if Path(path).exists() else []
        turn = turn_after(cache[path], e["prompt_sha"], near=e.get("ts"))
        if turn is None:
            continue
        calls = usage.calls(turn)
        k = len(calls)
        parent, helper = prices.get(e["model"]), prices.get(e["helper_model"])
        would_lose = None
        if parent and helper and k:
            scale = 1.0 if e["model"] == e["helper_model"] else delegation.OTHER_MODEL_SCALE
            keep = delegation.keep_cost(k, e["context"], e["added"], e["output"], parent)
            would_lose = (
                delegation.delegate_cost(k, e["context"], e["added"], e["output"], parent, helper, scale) > keep
            )
        rows.append(
            {
                "ts": e["ts"],
                "outcome": e["outcome"],
                "steps": e["steps"],
                "median_calls": e["median_calls"],
                "real_calls": k,
                "real_cost": sum(c.cost(prices) or 0.0 for c in calls),
                "would_lose": would_lose,
            }
        )
    return rows


def shadow_report(events: list[dict], prices: dict) -> str:
    seen = [e for e in events if e.get("version") == 3]
    decided = [e for e in seen if e.get("outcome") in ("keep", "delegate")]
    chosen = sum(e["outcome"] == "delegate" for e in decided)
    answered = [e for e in seen if e.get("answered")]
    share = f"({chosen / len(decided):.0%})" if decided else "(none yet)"
    lines = [
        "# Shadow report",
        "",
        f"Messages seen: {len(seen)}; decided: {len(decided)}; worth a fresh subagent: {chosen} {share}.",
        f"Jev: {len(answered)} answered calls, ${sum(e.get('cost') or 0 for e in answered):.4f}, "
        f"{sum(e.get('latency_ms') or 0 for e in seen) / 1000:.1f} s of added wait in total.",
    ]
    selected = [r for r in shadow_rows(seen, prices) if r["outcome"] == "delegate"]
    if selected:
        lost = sum(bool(r["would_lose"]) for r in selected)
        lines.append(
            f"Selected jobs found in their transcript: {len(selected)}. Median predicted "
            f"{statistics.median(r['median_calls'] for r in selected):.0f} calls, median real "
            f"{statistics.median(r['real_calls'] for r in selected):.0f}. At their real length the cost model says "
            f"{lost} ({lost / len(selected):.0%}) would have cost more delegated."
        )
    return "\n".join(lines)
