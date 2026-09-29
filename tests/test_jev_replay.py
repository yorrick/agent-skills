"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402
import replay  # noqa: E402
import snapshot  # noqa: E402
from test_jev_snapshot import EVENT, git, payload, write  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def test_restore_rebuilds_the_working_copy_with_a_local_origin(tmp_path: Path, repo: Path, snap: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    (repo / "later.py").write_text("x\n")
    git(repo, "add", "later.py")
    git(repo, "commit", "-qm", "later work")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert git(clone, "rev-parse", "HEAD").strip() == head
    assert (clone / "app.py").read_text() == "print('v2')\n"
    assert (clone / "notes.md").read_text() == "draft\n"
    assert not (clone / "later.py").exists()
    assert (clone / ".env").read_text() == "TOKEN=x\n"
    assert (clone / "node_modules" / ".package-lock.json").exists()
    assert git(clone, "remote", "get-url", "origin").strip() == str(tmp_path / "r" / "origin.git")
    assert git(clone, "status", "--porcelain").splitlines() == [" M app.py", "?? notes.md"]
    assert git(clone, "show", "refs/jev/start:app.py") == "print('v2')\n"
    assert git(clone, "show", "refs/jev/start:notes.md") == "draft\n"
    assert git(clone, "rev-parse", "refs/jev/start^").strip() == head
    # Ruling T9e: the bare copy has no configured path back to the source.
    assert git(tmp_path / "r" / "origin.git", "remote").strip() == ""


def test_changed_dependencies_make_the_job_inconclusive(tmp_path: Path, repo: Path, snap: Path) -> None:
    (repo / ".env").write_text("TOKEN=changed\n")
    with pytest.raises(replay.Inconclusive, match="changed since the snapshot"):
        replay.restore(snap, tmp_path / "r")


def test_source_info_exclude_ignores_carry_into_the_clone(tmp_path: Path, repo: Path, snap: Path) -> None:
    """A file matched only by the source's `.git/info/exclude` (never committed, so
    a plain clone never sees it) still needs to end up ignored in the clone too, or
    `git add -A` later would publish it."""
    common = git(repo, "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common) if Path(common).is_absolute() else repo / common
    (common_dir / "info" / "exclude").write_text("secret.local\n")
    (repo / "secret.local").write_text("shh\n")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (clone / "secret.local").read_text() == "shh\n"
    assert git(clone, "status", "--porcelain").splitlines() == [" M app.py", "?? notes.md"]


def test_nested_checkout_under_an_ignored_directory_is_pruned(tmp_path: Path, repo: Path, snap: Path) -> None:
    """`.claude/worktrees/` is a common ignore pattern, and a linked worktree
    inside it has a `.git` file with an absolute `gitdir:` back at the real
    repository. Any git command run inside a copy of it would then touch the
    real repository, so it must never be copied."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".claude/worktrees/\n")
    (repo / ".claude").mkdir()
    git(repo, "worktree", "add", "-q", "-b", "wt", str(repo / ".claude" / "worktrees" / "wt"))
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / ".claude" / "worktrees" / "wt").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["skipped"] == [".claude/worktrees/wt"]


def test_ignored_virtualenvs_are_not_copied(tmp_path: Path, repo: Path) -> None:
    """A copied `.venv` keeps absolute paths into the source checkout (bin/*
    shebangs, editable .pth/direct_url.json); `uv run` rebuilds it in the clone
    instead. The venv exists before the snapshot is taken, so the fingerprint
    check that catches drifted dependencies does not fire here; pruning it is a
    separate rule that applies even to a venv that has not changed at all."""
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /wherever\n")
    (venv / "python").write_text("#!/wherever/bin/python3\n")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / ".venv").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert ".venv" in restored["skipped"]


def test_force_added_tracked_file_survives_the_start_commit(tmp_path: Path, repo: Path) -> None:
    """`git add -A` into an index that starts empty would treat a force-added
    tracked file that matches an ignore pattern as a new, excluded, untracked
    path, and silently drop it from `refs/jev/start`."""
    (repo / "build").mkdir()
    (repo / "build" / "out.txt").write_text("built\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "build/\n")
    git(repo, "add", "-f", "build/out.txt")
    git(repo, "commit", "-qm", "force add a build artifact")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    assert git(clone, "show", "refs/jev/start:build/out.txt") == "built\n"


def test_ignore_rule_drift_since_the_snapshot_makes_it_inconclusive(tmp_path: Path, repo: Path) -> None:
    """A file tracked at snapshot time can become untracked and ignored later (a
    committed file the user removed from the index and added to .gitignore).
    Blindly copying the source's current version over the clone's checked-out
    one would silently rewrite the clone away from `head`, undetected unless
    the copy's effect on `git status` is checked."""
    (repo / "config.json").write_text("{}\n")
    git(repo, "add", "config.json")
    git(repo, "commit", "-qm", "add config")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    git(repo, "rm", "-q", "--cached", "config.json")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "config.json\n")
    (repo / "config.json").write_text('{"changed": true}\n')  # the user moved on after the snapshot
    with pytest.raises(replay.Inconclusive, match="ignored files changed since the snapshot"):
        replay.restore(snap, tmp_path / "r")


def test_check_marks_a_good_snapshot_restore_ok(tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["check"]) == 0
    assert fork_check.statuses()[snap.name]["status"] == "restore_ok"
    assert not (tmp_path / "fc" / "checks" / snap.name).exists()


def test_check_marks_a_broken_snapshot_restore_failed(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (snap / "changes.diff").write_bytes(b"not a real diff\n")
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["check"]) == 0
    assert fork_check.statuses()[snap.name]["status"] == "restore_failed"
    assert not (tmp_path / "fc" / "checks" / snap.name).exists()
