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


def period_events(events: list[dict], points: list[dict], started: str | None = None) -> list[dict]:
    """Every cache-aware hook event of the check, from whichever came first of
    `started` (when capture mode was set, so Jev's calls before the first scored
    job count too) and the first scored job's capture, to the last one's plus a
    minute: the last hook logs its event just after its snapshot."""
    if not points:
        return []
    first = datetime.fromisoformat(points[0]["meta"]["created"])
    if started:
        first = min(first, datetime.fromisoformat(started))
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


POINTS = 20


def _broken(verdict: dict) -> bool:
    """A delegated result that fails a test or its goal where the kept one passes."""
    keep, delegate = verdict["keep"], verdict["delegate"]
    return (delegate["tests"] == "fail" and keep["tests"] != "fail") or (
        not delegate["outcome_met"] and keep["outcome_met"]
    )


def _listed(title: str, jobs: list[tuple[str, str]]) -> list[str]:
    if not jobs:
        return []
    return [title, *[f"- {sid}: {reason or 'no reason given'}" for sid, reason in jobs], ""]


def check_report(
    points: list[dict],
    events: list[dict],
    skipped: list[tuple[str, str]],
    inconclusive: list[tuple[str, str]],
    period_cost: float | None = None,
    waiting: tuple[str, ...] = (),
    capture_started: str | None = None,
) -> tuple[str, bool]:
    """`skipped` and `inconclusive` are (job, reason) pairs from the same window
    as `points`: capture order, up to the 20th eligible job."""
    points = points[:POINTS]
    keep_cost = sum(p["result"]["sides"]["keep"]["cost"] for p in points)
    del_cost = sum(p["result"]["sides"]["delegate"]["cost"] for p in points)
    keep_time = sum(p["result"]["sides"]["keep"]["wall_seconds"] for p in points)
    del_time = sum(p["result"]["sides"]["delegate"]["wall_seconds"] for p in points)
    period = period_events(events, points, capture_started)
    del_cost += sum(e.get("cost") or 0 for e in period)
    del_time += sum(e.get("latency_ms") or 0 for e in period) / 1000
    broken = [p for p in points if _broken(p["verdict"])]
    prefer_keep = sum(p["verdict"]["prefer"] == "keep" for p in points)
    prefer_delegate = sum(p["verdict"]["prefer"] == "delegate" for p in points)
    overrides = sum(not p["result"]["sides"]["delegate"]["delegated"] for p in points)
    conditions = {
        f"{POINTS} jobs, all judged": len(points) == POINTS and not waiting,
        "at least 10% cheaper": bool(points) and del_cost <= 0.9 * keep_cost,
        "as good": not broken and prefer_keep <= prefer_delegate,
        "not slower": bool(points) and del_time <= keep_time,
    }
    lines = [
        "# Fork check",
        "",
        f"Jobs judged: {len(points)} of {POINTS}"
        + (f", waiting for {', '.join(waiting)}" if waiting else "")
        + f". Skipped by you: {len(skipped)}. Inconclusive: {len(inconclusive)}. "
        f"Overrides (the session kept a job it was told to hand off): {overrides}.",
        f"Cost: keep ${keep_cost:.2f}, delegate ${del_cost:.2f} with Jev's cost over the period included.",
        f"Time: keep {keep_time / 60:.0f} min, delegate {del_time / 60:.0f} min with Jev's added wait included.",
        f"Blind judge: prefers keep {prefer_keep}, delegate {prefer_delegate}; "
        f"broken delegated results: {len(broken)}.",
        *(
            [
                f"Saving as a share of the period's decided messages (their main-thread cost): "
                f"{(keep_cost - del_cost) / period_cost:.0%} (no threshold: it depends on how the work mixes long and "
                "short jobs)."
            ]
            if period_cost
            else []
        ),
        "",
        *[f"- {name}: {'pass' if ok else 'FAIL'}" for name, ok in conditions.items()],
        "",
        *_listed("Skipped by you:", skipped),
        *_listed("Inconclusive:", inconclusive),
        "| job | predicted calls | calls keep / delegate | cost keep / delegate | minutes keep / delegate | judge |",
        "|---|---|---|---|---|---|",
    ]
    for p in points:
        k, d = p["result"]["sides"]["keep"], p["result"]["sides"]["delegate"]
        lines.append(
            f"| {p['meta']['id']} | {p['meta'].get('median_calls')} | {k['calls']} / {d['calls']} | "
            f"${k['cost']:.2f} / ${d['cost']:.2f} | {k['wall_seconds'] / 60:.0f} / {d['wall_seconds'] / 60:.0f} | "
            f"{p['verdict']['prefer']} |"
        )
    return "\n".join(lines), all(conditions.values())
