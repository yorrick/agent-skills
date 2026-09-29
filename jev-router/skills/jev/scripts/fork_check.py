#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""The fork check: replay the jobs the router would delegate, both ways, and report.

fork_check.py shadow                     shadow-mode decisions against what really happened
fork_check.py list                       snapshots, their status, and what each real turn did outside
fork_check.py check                      restore-check new snapshots without running anything
fork_check.py mark ID (safe|skip) [--reason TEXT]
fork_check.py replay (ID | --next) [--trial]
fork_check.py judge ID                   blind Codex judge (before publishing)
fork_check.py publish ID                 push both results to a private copy and open the comparison PR
fork_check.py report                     the fork check's pass or fail over 20 jobs
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import jev_router
import report
import usage

EXTERNAL_SHELL = re.compile(
    r"\bgit\s+push\b|\bgh\s+(pr|issue|release|repo|secret|workflow)\s+(create|merge|edit|close|comment|delete|run|set)"
    r"|\bgh\s+api\b.*-X\s*(POST|PUT|PATCH|DELETE)|\bvercel\b|\bsupabase\s+(db\s+push|functions\s+deploy)"
    r"|\bpulumi\s+up\b|\bterraform\s+apply\b|\bcurl\b.*-X\s*(POST|PUT|PATCH|DELETE)|\bhttp\s+(POST|PUT|PATCH|DELETE)\b"
    r"|\bnpm\s+publish\b|\bdeploy\b",
    re.IGNORECASE,
)
# Reason: an MCP tool is listed unless its name says it only reads; the user decides.
READ_ONLY_MCP = re.compile(r"__(get|list|search|read|query_logs|fetch|describe|view|find)[_a-z]*$", re.IGNORECASE)


def external_actions(turn: list[dict]) -> list[str]:
    """What a turn did outside the machine: a push, a PR/issue/deploy command, or a
    non-read MCP tool call. A guide for the user's safe/skip mark, not a guarantee."""
    found = []
    for entry in turn:
        if entry.get("type") != "assistant":
            continue
        for block in (entry.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, inputs = str(block.get("name")), block.get("input") or {}
            if name == "Bash" and EXTERNAL_SHELL.search(str(inputs.get("command", ""))):
                found.append(f"shell: {str(inputs['command'])[:120]}")
            elif name.startswith("mcp__") and not READ_ONLY_MCP.search(name):
                found.append(f"MCP: {name}")
    return found


def real_turn(meta: dict, message: str) -> list[dict]:
    """The real turn in the user's session, plus its subagents' entries in the same
    time range. Reads the live transcript named by meta.json, not the snapshot's own
    frozen copy, since only the live one holds what actually followed the prompt."""
    path = Path(meta["transcript_path"])
    entries = usage.read_entries(path) if path.exists() else []
    turn = report.turn_after(entries, usage.prompt_sha(message), near=meta["created"])
    if not turn:
        return []
    stamps = [e["timestamp"] for e in turn if isinstance(e.get("timestamp"), str)]
    subagents = path.with_suffix("") / "subagents"
    if stamps and subagents.exists():
        for f in sorted(subagents.glob("*.jsonl")):
            turn += [e for e in usage.read_entries(f) if stamps[0] <= str(e.get("timestamp", "")) <= stamps[-1]]
    return turn


def root() -> Path:
    """Where snapshots live. Delegates to jev_router so the hook (which writes
    snapshots) and this runner (which reads them) can never disagree."""
    return jev_router.fork_check_dir(jev_router.load_config())


def set_status(sid: str, status: str, reason: str = "") -> None:
    path = root() / "status.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "id": sid, "status": status, "reason": reason}
    with path.open("a") as handle:
        handle.write(json.dumps(line) + "\n")


def statuses() -> dict[str, dict]:
    path = root() / "status.jsonl"
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            latest[entry["id"]] = entry
    return latest


def snapshot_dirs() -> list[Path]:
    """Finished snapshots only: a folder named `<id>.tmp` is one a hook is still
    writing, or abandoned mid-write, even if it already holds a `meta.json`."""
    return sorted(
        p for p in (root() / "snapshots").glob("*") if not p.name.endswith(".tmp") and (p / "meta.json").exists()
    )


def cmd_shadow() -> int:
    print(report.shadow_report(jev_router.read_log(), usage.load_prices()))
    return 0


def cmd_list() -> int:
    """Every finished snapshot, its status, and what its real turn did outside the
    machine, so the user can decide which jobs are safe to replay with full access."""
    done = statuses()
    print("| snapshot | repo | expected saving | status | outside the machine (from the real turn) |")
    print("|---|---|---|---|---|")
    for snap in snapshot_dirs():
        meta = json.loads((snap / "meta.json").read_text())
        actions = external_actions(real_turn(meta, (snap / "message.txt").read_text()))
        shown = "; ".join(actions[:3]) + (f"; and {len(actions) - 3} more" if len(actions) > 3 else "")
        status = done.get(snap.name, {}).get("status", "new")
        print(
            f"| {snap.name} | {Path(meta['toplevel']).name} | ${meta['expected_saving']:.2f} | {status} | "
            f"{shown or 'nothing found'} |"
        )
    print(
        "\nThe list is a guide: a replay can do something the real turn did not. Mark a job safe only if two "
        "replays in a row could run without an external effect you would mind."
    )
    snapshots_dir = root() / "snapshots"
    abandoned = sorted(snapshots_dir.glob("*.tmp")) if snapshots_dir.exists() else []
    if abandoned:
        print(f"\n{len(abandoned)} abandoned snapshot folders (*.tmp) under {snapshots_dir}; safe to delete.")
    return 0


def cmd_mark(sid: str, mark: str, reason: str) -> int:
    set_status(sid, mark, reason)
    print(f"{sid}: {mark}")
    return 0


def cmd_check() -> int:
    """Restore every snapshot not yet checked, without running anything, and compare
    it: the commit, the uncommitted changes and the untracked files."""
    import shutil
    import tarfile

    import replay

    done = statuses()
    for snap in snapshot_dirs():
        sid = snap.name
        if sid in done:
            continue
        dest = root() / "checks" / sid
        # Reason: a folder a killed run left behind would otherwise make
        # `restore`'s `dest.mkdir` fail forever, permanently marking this snapshot
        # restore_failed instead of retrying it clean next time.
        shutil.rmtree(dest, ignore_errors=True)
        try:
            clone = replay.restore(snap, dest, copy_ignored=False)
            meta = json.loads((snap / "meta.json").read_text())
            with tarfile.open(snap / "untracked.tar") as tar:
                captured = sorted(tar.getnames())
            listed = replay.run("git", "-C", str(clone), "ls-files", "--others", "--exclude-standard", "-z")
            ok = (
                replay.run("git", "-C", str(clone), "rev-parse", "HEAD").strip() == meta["head"]
                and replay.run("git", "-C", str(clone), "diff", "--binary", "HEAD")
                == (snap / "changes.diff").read_text()
                and sorted(p for p in listed.split("\0") if p) == captured
            )
            set_status(sid, "restore_ok" if ok else "restore_failed", "" if ok else "restored state differs")
        except Exception as exc:  # report and move on to the next snapshot
            set_status(sid, "restore_failed", str(exc)[:200])
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        print(f"{sid}: {statuses()[sid]['status']}")
    return 0


def cmd_replay(sid: str | None, trial: bool) -> int:
    import random
    import shutil

    import replay

    done = statuses()
    if sid is None:
        todo = [p.name for p in snapshot_dirs() if done.get(p.name, {}).get("status") == "safe"]
        if not todo:
            print("No snapshot is marked safe and waiting. Mark one with: mark ID safe")
            return 1
        sid = todo[0]
    elif done.get(sid, {}).get("status") != "safe":
        print(f"{sid} is not marked safe; mark it first.")
        return 1
    snap, out = root() / "snapshots" / sid, root() / "results" / sid
    # Reason: a previous run that ended in replay_failed may have left partial
    # clones here; restore()'s dest.mkdir(parents=True) would fail forever
    # otherwise, permanently jamming this snapshot.
    shutil.rmtree(out, ignore_errors=True)
    try:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        claude_home = Path(configured) if configured else replay.DEFAULT_CLAUDE_HOME
        result = replay.replay_pair(snap, out, claude_home, random.Random())
    except replay.Inconclusive as exc:
        set_status(sid, "inconclusive", str(exc))
        print(f"{sid}: inconclusive ({exc})")
        return 0
    except Exception as exc:
        set_status(sid, "replay_failed", str(exc)[:200])
        print(f"{sid}: replay failed ({exc})")
        return 1
    result["trial"] = trial
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    reason = result.get("reason", "")
    set_status(sid, "inconclusive" if result["inconclusive"] else "replayed", reason)
    if reason:
        print(f"{sid}: inconclusive ({reason})")
    if reason == "a replay used a path into the real repository":
        print(f"WARNING: {sid} replay used a path into the real repository; discarded.")
    for name in result["order"]:
        s = result["sides"].get(name)
        if s is None:
            continue
        print(
            f"{name}: ${s.get('cost', 0.0):.2f}, {s.get('wall_seconds', 0.0):.0f} s, {s.get('calls', 0)} calls, "
            f"warm={s['warm']}" + ("" if name == "keep" else f", handed to the helper={s.get('delegated', False)}")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shadow")
    sub.add_parser("check")
    sub.add_parser("list")
    mark = sub.add_parser("mark")
    mark.add_argument("id")
    mark.add_argument("mark", choices=("safe", "skip"))
    mark.add_argument("--reason", default="")
    rep = sub.add_parser("replay")
    target = rep.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?")
    target.add_argument("--next", action="store_true")
    rep.add_argument("--trial", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "shadow":
        return cmd_shadow()
    if args.command == "check":
        return cmd_check()
    if args.command == "list":
        return cmd_list()
    if args.command == "mark":
        return cmd_mark(args.id, args.mark, args.reason)
    if args.command == "replay":
        return cmd_replay(None if args.next else args.id, args.trial)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
