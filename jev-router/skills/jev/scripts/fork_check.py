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
from datetime import UTC, datetime
from pathlib import Path

import jev_router
import report
import usage


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shadow")
    sub.add_parser("check")
    args = parser.parse_args(argv)
    if args.command == "shadow":
        return cmd_shadow()
    if args.command == "check":
        return cmd_check()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
