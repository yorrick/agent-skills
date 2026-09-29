"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402
import replay  # noqa: E402
from test_jev_snapshot import git  # noqa: E402


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


def test_check_marks_a_good_snapshot_restore_ok(tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["check"]) == 0
    assert fork_check.statuses()[snap.name]["status"] == "restore_ok"
    assert not (tmp_path / "fc" / "checks" / snap.name).exists()
