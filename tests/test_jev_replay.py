"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import json
import os
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


# --- fix round 2: symlinked ignored entries (Ruling T9f, repro.py) -------------------


def test_symlinked_ignored_entry_through_a_parent_dir_leaves_the_real_dirs_untouched(
    tmp_path: Path, repo: Path, snap: Path
) -> None:
    """repro.py mode "parent": an ignore pattern can match a symlink to a
    directory entirely outside the repository that itself holds a nested
    checkout and a venv. `cp -cRp` keeps the symlink, and a naive walk would
    follow it into the user's real filesystem and delete real directories there;
    this must never be touched, let alone removed."""
    ext = tmp_path / "ext"
    (ext / "subrepo").mkdir(parents=True)
    git(ext / "subrepo", "init", "-q")
    (ext / "subrepo" / "precious.txt").write_text("user work\n")
    (ext / "venv").mkdir()
    (ext / "venv" / "pyvenv.cfg").write_text("home=/x\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "data\n")
    (repo / "data").symlink_to(ext)  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (ext / "subrepo").exists()
    assert (ext / "venv").exists()
    assert not (clone / "data").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["skipped"] == ["data"]


def test_symlinked_special_dir_is_skipped_without_raising(tmp_path: Path, repo: Path, snap: Path) -> None:
    """repro.py mode "self": the symlink points directly at a special dir (here a
    nested checkout; a symlinked `.venv` or an `npm link`ed package is the same
    shape). Deleting through a symlink like that used to raise a raw OSError;
    the fix must never even attempt to copy it."""
    ext = tmp_path / "ext"
    (ext / "subrepo").mkdir(parents=True)
    git(ext / "subrepo", "init", "-q")
    (ext / "subrepo" / "precious.txt").write_text("user work\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "data\n")
    (repo / "data").symlink_to(ext / "subrepo")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (ext / "subrepo").exists()
    assert not (clone / "data").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["skipped"] == ["data"]


def test_relative_symlink_inside_an_ignored_entry_staying_in_the_repo_is_copied_as_a_link(
    tmp_path: Path, repo: Path
) -> None:
    """A relative symlink inside an ignored directory that resolves inside the
    repository is safe: `_collect_exclusions` must not flag it, and it must
    reach the clone as a link, not be expanded or skipped."""
    (repo / "node_modules" / ".bin").mkdir(parents=True)
    (repo / "node_modules" / "real-target.js").write_text("module.exports = 1;\n")
    (repo / "node_modules" / ".bin" / "tool").symlink_to(Path("..") / "real-target.js")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    link = clone / "node_modules" / ".bin" / "tool"
    assert link.is_symlink()
    assert os.readlink(link) == str(Path("..") / "real-target.js")
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["skipped"] == []


def test_stray_git_guard_rejects_a_surviving_nested_checkout(tmp_path: Path) -> None:
    """Direct unit test of the defense-in-depth backstop, independent of whether
    the normal `restore` flow can still trigger it."""
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "vendor" / "sub" / ".git").mkdir(parents=True)
    with pytest.raises(replay.Inconclusive, match="nested checkout survived"):
        replay._reject_stray_checkouts(clone)


# --- fix round 2: ignore-rule drift the porcelain diff alone cannot see (Ruling T9b, drift.py) ---


def test_ignored_untracked_file_edited_after_the_snapshot_is_inconclusive(tmp_path: Path, repo: Path) -> None:
    """drift.py mode "untracked-file": a file untracked and not ignored at
    snapshot time (captured in untracked.tar) becomes ignored and is edited
    afterward. `git status --porcelain` shows the same "?? scratch.txt" line
    before and after copying regardless of content, so only checking that the
    ignored path does not already exist in the clone catches this."""
    (repo / "scratch.txt").write_text("v1\n")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "scratch.txt\n")
    (repo / "scratch.txt").write_text("v2 AFTER SNAPSHOT\n")  # the user moved on after the snapshot
    with pytest.raises(replay.Inconclusive, match="already exists in the clone"):
        replay.restore(snap, tmp_path / "r")


def test_ignored_untracked_dir_edited_after_the_snapshot_is_inconclusive(tmp_path: Path, repo: Path) -> None:
    """drift.py mode "untracked-dir": an untracked directory captured in the tar
    is later matched by an ignore pattern. Copying it again on top would produce
    a `notes/notes/a.md` duplicate that `git status --porcelain` also cannot
    see, since it still collapses the whole directory into one "?? notes/"
    line."""
    (repo / "notes").mkdir()
    (repo / "notes" / "a.md").write_text("v1\n")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "notes/\n")
    (repo / "notes" / "a.md").write_text("v2 AFTER SNAPSHOT\n")  # the user moved on after the snapshot
    with pytest.raises(replay.Inconclusive, match="already exists in the clone"):
        replay.restore(snap, tmp_path / "r")


def test_modified_tracked_file_later_ignored_and_edited_is_inconclusive(tmp_path: Path, repo: Path) -> None:
    """drift.py mode "modified-tracked": a file tracked and modified at snapshot
    time (captured in changes.diff) is later untracked, ignored and edited
    again. `git status --porcelain` shows the same " M config.json" line before
    and after copying regardless of which modified content is on disk."""
    (repo / "config.json").write_text("{}\n")
    git(repo, "add", "config.json")
    git(repo, "commit", "-qm", "add config")
    (repo / "config.json").write_text('{"v": 1}\n')  # snapshot-time uncommitted change
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    git(repo, "rm", "-q", "--cached", "config.json")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "config.json\n")
    (repo / "config.json").write_text('{"v": 2, "AFTER": "SNAPSHOT"}\n')  # the user moved on after the snapshot
    with pytest.raises(replay.Inconclusive, match="already exists in the clone"):
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


# --- Task 11: replay both sides and measure them -------------------------------------

import random  # noqa: E402

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FAKE.chmod(0o755)
    monkeypatch.setenv("JEV_FORK_CHECK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    return tmp_path / "claude-home"


def test_install_session_rewrites_paths_and_ids(tmp_path: Path, repo: Path, snap: Path) -> None:
    clone = replay.restore(snap, tmp_path / "r")
    sid = replay.install_session(snap, clone, tmp_path / "claude-home")
    text = (replay.project_dir(tmp_path / "claude-home", clone) / f"{sid}.jsonl").read_text()
    assert str(repo.resolve()) not in text
    assert f"Edited {clone}/app.py" in text
    assert all(json.loads(line).get("sessionId", sid) == sid for line in text.splitlines())


def test_pair_runs_both_sides_and_only_delegate_gets_the_note(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    keep, delegate = result["sides"]["keep"], result["sides"]["delegate"]
    assert keep["warm"] and delegate["warm"]
    assert delegate["delegated"] is True and keep["delegated"] is False
    assert delegate["calls"] == 2 and keep["calls"] == 1
    assert Path(keep["clone"], "RESULT.txt").read_text() == "done by keep\n"
    assert keep["skipped"] == []
    under_work = [str(Path(side["clone"]).relative_to(tmp_path / "work")) for side in (keep, delegate)]
    assert not any("keep" in p or "delegate" in p for p in under_work)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert all(c["router"] == "off" for c in calls)
    assert [c["note"] is not None for c in calls if not c["args"][c["args"].index("-p") + 1].startswith("Reply")] == [
        name == "delegate" for name in result["order"]
    ]


def test_cold_side_is_retried_then_inconclusive(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_COLD", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"]
    assert {s["attempt"] for s in result["sides"].values()} == {replay.ATTEMPTS}
