"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import types
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402
import replay  # noqa: E402
import snapshot  # noqa: E402
import usage  # noqa: E402
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


def _file_inodes(root: Path) -> set[tuple[int, int]]:
    return {(st.st_dev, st.st_ino) for p in root.rglob("*") if p.is_file() and not p.is_symlink() for st in [p.lstat()]}


def test_restore_shares_no_object_file_with_the_source(tmp_path: Path, repo: Path, snap: Path) -> None:
    """A local `git clone` hard-links the object files it copies, so a chmod or an
    in-place write inside a clone would reach the user's real `.git/objects`.
    Nothing restore makes (the bare copy, or the clone of it) may share an inode
    with the source repository's objects."""
    source = _file_inodes(repo / ".git" / "objects")
    assert source
    replay.restore(snap, tmp_path / "r")
    assert _file_inodes(tmp_path / "r" / "origin.git" / "objects")
    assert not source & _file_inodes(tmp_path / "r")


def test_restore_from_a_source_that_borrows_objects_borrows_nothing(tmp_path: Path, repo: Path) -> None:
    """A source made with `git clone --shared` (or `--reference`) borrows objects
    through `objects/info/alternates`, and a plain local clone of it, even with
    `--no-hardlinks`, would keep borrowing from the same store. The bare copy
    and the clone of it must hold every object themselves: no alternates file,
    a clean fsck even once the lender's objects are gone, and no inode shared
    with either repository."""
    borrower = tmp_path / "work" / "borrower"
    subprocess.run(["git", "clone", "-q", "--shared", str(repo), str(borrower)], check=True)
    assert (borrower / ".git" / "objects" / "info" / "alternates").exists()
    (borrower / "app.py").write_text("print('v3')\n")
    git(borrower, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qam", "borrower's own commit")
    snap = take(tmp_path, borrower)
    replay.restore(snap, tmp_path / "r")
    bare, clone = tmp_path / "r" / "origin.git", tmp_path / "r" / "repo"
    assert not (bare / "objects" / "info" / "alternates").exists()
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()
    restored = _file_inodes(tmp_path / "r")
    assert not restored & (_file_inodes(repo / ".git" / "objects") | _file_inodes(borrower / ".git" / "objects"))
    # Reason: with the lender's store gone, only objects the copies hold themselves remain.
    (repo / ".git" / "objects").rename(tmp_path / "lender-objects")
    for copy in (bare, clone):
        git(copy, "fsck", "--full")
        git(copy, "cat-file", "-e", "HEAD^{tree}")
    assert git(clone, "show", "HEAD:app.py") == "print('v3')\n"


def test_changed_dependencies_make_the_job_inconclusive(tmp_path: Path, repo: Path, snap: Path) -> None:
    (repo / ".env").write_text("TOKEN=changed\n")
    with pytest.raises(replay.Inconclusive, match="changed since the snapshot"):
        replay.restore(snap, tmp_path / "r")


def take(tmp_path: Path, repo: Path) -> Path:
    """A snapshot of `repo` as it is now, for a test that sets the repo up first."""
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    return tmp_path / "fc" / "snapshots" / sid


def exclude_file(repo: Path) -> Path:
    common = git(repo, "rev-parse", "--git-common-dir").strip()
    return (Path(common) if Path(common).is_absolute() else repo / common) / "info" / "exclude"


def test_source_info_exclude_ignores_carry_into_the_clone(tmp_path: Path, repo: Path) -> None:
    """A file matched only by the source's `.git/info/exclude` (never committed, so
    a plain clone never sees it) still needs to end up ignored in the clone too, or
    `git add -A` later would publish it."""
    exclude_file(repo).write_text("secret.local\n")
    (repo / "secret.local").write_text("shh\n")
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
    assert (clone / "secret.local").read_text() == "shh\n"
    assert git(clone, "status", "--porcelain").splitlines() == [" M app.py", "?? notes.md"]


# --- Ruling F3': only the ignored entries captured with the snapshot are copied ----


def test_a_file_added_to_info_exclude_after_capture_is_not_copied(tmp_path: Path, repo: Path, snap: Path) -> None:
    exclude_file(repo).write_text("secret.local\n")
    (repo / "secret.local").write_text("shh\n")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / "secret.local").exists()
    assert (clone / ".env").read_text() == "TOKEN=x\n"  # captured, still copied
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["not_captured"] == ["secret.local"]
    assert restored["ignored"] == [".env", "node_modules"] and restored["missing"] == []


def test_a_new_cache_is_not_copied_and_does_not_make_the_job_inconclusive(
    tmp_path: Path, repo: Path, snap: Path
) -> None:
    cache = repo / ".pytest_cache"
    (cache / "v").mkdir(parents=True)
    (cache / ".gitignore").write_text("*\n")  # what pytest writes, so git ignores the whole folder
    (cache / "v" / "lastfailed").write_text("{}\n")
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / ".pytest_cache").exists()
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["not_captured"] == [".pytest_cache"]


def test_a_captured_entry_gone_since_is_listed_as_missing(tmp_path: Path, repo: Path) -> None:
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "*.log\n")
    (repo / "build.log").write_text("old build\n")
    snap = take(tmp_path, repo)
    (repo / "build.log").unlink()  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / "build.log").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["missing"] == ["build.log"] and "build.log" not in restored["ignored"]


def test_an_ignored_entry_touched_after_capture_is_not_copied(tmp_path: Path, repo: Path) -> None:
    """F19: a file the real turn added inside a captured `.cache/` keeps the whole
    folder out and lists it; the job stays usable. A dependency folder is
    copied anyway, since the fingerprint watches it."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".cache/\n")
    (repo / ".cache").mkdir()
    (repo / ".cache" / "a.txt").write_text("cached before\n")
    (repo / "node_modules" / "pkg").mkdir()
    (repo / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
    snap = take(tmp_path, repo)
    later = datetime.fromisoformat(json.loads((snap / "meta.json").read_text())["created"]).timestamp() + 60
    added = repo / ".cache" / "b.txt"
    added.write_text("written by the real turn\n")
    os.utime(added, (later, later))
    os.utime(repo / "node_modules" / "pkg" / "index.js", (later, later))
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / ".cache").exists()
    assert (clone / "node_modules" / "pkg" / "index.js").exists()
    assert (clone / ".env").read_text() == "TOKEN=x\n"
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["changed_since_capture"] == [".cache"]
    assert restored["ignored"] == [".env", "node_modules"]


def test_a_change_within_the_capture_second_is_seen(tmp_path: Path, repo: Path) -> None:
    """F23b: `created` is kept to the microsecond and is the cutoff itself, so a
    change a fifth of a second after the capture is not copied."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".cache/\n")
    (repo / ".cache").mkdir()
    (repo / ".cache" / "a.txt").write_text("cached before\n")
    snap = take(tmp_path, repo)
    soon = datetime.fromisoformat(json.loads((snap / "meta.json").read_text())["created"]).timestamp() + 0.2
    (repo / ".cache" / "a.txt").write_text("rewritten by the real turn\n")
    for path in (repo / ".cache" / "a.txt", repo / ".cache"):
        os.utime(path, (soon, soon))
    clone = replay.restore(snap, tmp_path / "r")
    assert not (clone / ".cache").exists()
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["changed_since_capture"] == [".cache"]


def test_an_entry_that_vanishes_while_it_is_looked_at_counts_as_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N4: a file removed between the walk and its lstat is being changed right now."""
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / "gone.txt").write_text("x\n")
    real_lstat = os.lstat

    def vanishing(path: object) -> os.stat_result:
        if str(path).endswith("gone.txt"):
            raise FileNotFoundError(path)
        return real_lstat(path)  # type: ignore[arg-type]

    far_future = time.time() + 3600
    assert not replay._touched_since(folder, far_future)  # the file is still there
    monkeypatch.setattr(replay.os, "lstat", vanishing)
    assert replay._touched_since(folder, far_future)


def test_a_snapshot_without_its_ignored_entries_is_refused(tmp_path: Path, snap: Path) -> None:
    meta = json.loads((snap / "meta.json").read_text())
    del meta["ignored_entries"]
    (snap / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(replay.Inconclusive, match="no list of ignored entries"):
        replay.restore(snap, tmp_path / "r")
    assert not (tmp_path / "r").exists()


def test_a_captured_entry_already_in_the_clone_is_still_refused(tmp_path: Path, snap: Path) -> None:
    """The collision check stays as a backstop, here reached with a tampered list."""
    meta = json.loads((snap / "meta.json").read_text())
    meta["ignored_entries"] = [*meta["ignored_entries"], "notes.md"]  # notes.md comes from the tar
    (snap / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(replay.Inconclusive, match="notes.md already exists in the clone"):
        replay.restore(snap, tmp_path / "r")


def test_nested_checkout_under_an_ignored_directory_is_pruned(tmp_path: Path, repo: Path) -> None:
    """`.claude/worktrees/` is a common ignore pattern, and a linked worktree
    inside it has a `.git` file with an absolute `gitdir:` back at the real
    repository. Any git command run inside a copy of it would then touch the
    real repository, so it must never be copied."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".claude/worktrees/\n")
    (repo / ".claude").mkdir()
    git(repo, "worktree", "add", "-q", "-b", "wt", str(repo / ".claude" / "worktrees" / "wt"))
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
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
    tmp_path: Path, repo: Path
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
    (repo / "data").symlink_to(ext)
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
    assert (ext / "subrepo").exists()
    assert (ext / "venv").exists()
    assert not (clone / "data").exists()
    restored = json.loads((tmp_path / "r" / "restore.json").read_text())
    assert restored["skipped"] == ["data"]


def test_symlinked_special_dir_is_skipped_without_raising(tmp_path: Path, repo: Path) -> None:
    """repro.py mode "self": the symlink points directly at a special dir (here a
    nested checkout; a symlinked `.venv` or an `npm link`ed package is the same
    shape). Deleting through a symlink like that used to raise a raw OSError;
    the fix must never even attempt to copy it."""
    ext = tmp_path / "ext"
    (ext / "subrepo").mkdir(parents=True)
    git(ext / "subrepo", "init", "-q")
    (ext / "subrepo" / "precious.txt").write_text("user work\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "data\n")
    (repo / "data").symlink_to(ext / "subrepo")
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
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


# --- ignore-rule drift after the capture (Ruling T9b, then F3') ------------------------
# A path that became ignored only after the capture is not in the captured list, so
# restore never copies it: the clone keeps the snapshot's own version, which is the
# faithful one, and the path is listed under "not_captured".


def test_untracked_file_ignored_and_edited_after_the_snapshot_keeps_its_captured_version(
    tmp_path: Path, repo: Path
) -> None:
    """drift.py mode "untracked-file": a file untracked and not ignored at
    snapshot time (captured in untracked.tar) becomes ignored and is edited."""
    (repo / "scratch.txt").write_text("v1\n")
    snap = take(tmp_path, repo)
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "scratch.txt\n")
    (repo / "scratch.txt").write_text("v2 AFTER SNAPSHOT\n")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (clone / "scratch.txt").read_text() == "v1\n"
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["not_captured"] == ["scratch.txt"]


def test_untracked_dir_ignored_and_edited_after_the_snapshot_keeps_its_captured_version(
    tmp_path: Path, repo: Path
) -> None:
    """drift.py mode "untracked-dir": copying the folder again on top would have
    made a `notes/notes/a.md` duplicate that `git status --porcelain` cannot see."""
    (repo / "notes").mkdir()
    (repo / "notes" / "a.md").write_text("v1\n")
    snap = take(tmp_path, repo)
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "notes/\n")
    (repo / "notes" / "a.md").write_text("v2 AFTER SNAPSHOT\n")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (clone / "notes" / "a.md").read_text() == "v1\n"
    assert not (clone / "notes" / "notes").exists()
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["not_captured"] == ["notes"]


def test_modified_tracked_file_later_ignored_and_edited_keeps_its_captured_version(tmp_path: Path, repo: Path) -> None:
    """drift.py mode "modified-tracked": a file tracked and modified at snapshot
    time (captured in changes.diff) is later untracked, ignored and edited."""
    (repo / "config.json").write_text("{}\n")
    git(repo, "add", "config.json")
    git(repo, "commit", "-qm", "add config")
    (repo / "config.json").write_text('{"v": 1}\n')  # snapshot-time uncommitted change
    snap = take(tmp_path, repo)
    git(repo, "rm", "-q", "--cached", "config.json")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "config.json\n")
    (repo / "config.json").write_text('{"v": 2, "AFTER": "SNAPSHOT"}\n')  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert (clone / "config.json").read_text() == '{"v": 1}\n'
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["not_captured"] == ["config.json"]


# --- Ruling F1: a symlink in the start state that points out of the clone ------------


@pytest.mark.parametrize(("name", "how"), [("data", "absolute"), ("up", "../.."), ("data", "staged")])
def test_a_symlink_out_of_the_clone_is_inconclusive(tmp_path: Path, repo: Path, name: str, how: str) -> None:
    """A committed `data -> <absolute path>` would let a replay write straight
    through it into the real checkout, and the leak scan would never see the
    real path spelled out; `up -> ../..` escapes by climbing instead. One only
    staged, not committed, reaches the clone through changes.diff instead."""
    ext = tmp_path / "ext"
    ext.mkdir()
    (repo / name).symlink_to(ext if how in ("absolute", "staged") else how)
    git(repo, "add", name)
    if how != "staged":
        git(repo, "commit", "-qm", "a link")
    snap = take(tmp_path, repo)
    with pytest.raises(replay.Inconclusive, match=f"the symlink {name} points outside the clone"):
        replay.restore(snap, tmp_path / "r")
    assert list(ext.iterdir()) == []


def test_a_tracked_symlink_inside_the_clone_is_kept(tmp_path: Path, repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "main.py").symlink_to(Path("..") / "app.py")
    git(repo, "add", "docs")
    git(repo, "commit", "-qm", "a link")
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
    assert os.readlink(clone / "docs" / "main.py") == str(Path("..") / "app.py")


# --- the Task 9 minors (final fix wave A2) --------------------------------------------


def test_a_link_that_climbs_above_the_top_and_comes_back_is_not_copied(tmp_path: Path, repo: Path) -> None:
    """`../../app/app.py` from `app/node_modules` resolves inside the source, but
    only by going through the folder's own name: in a clone named `repo` it
    points at a sibling `app` folder outside the clone."""
    link = repo / "node_modules" / "back"
    link.symlink_to(Path("..") / ".." / repo.name / "app.py")
    assert not replay._resolves_inside(repo, link)
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
    assert not os.path.lexists(clone / "node_modules" / "back")
    assert json.loads((tmp_path / "r" / "restore.json").read_text())["skipped"] == ["node_modules/back"]


def test_a_copy_through_a_folder_linked_out_of_the_clone_is_refused(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth behind F1: a folder on the way to a captured entry is a
    link out of the clone (planted here after F1's check ran), so the copy
    would write through it."""
    (repo / "logs").mkdir()
    (repo / "logs" / "keep.txt").write_text("tracked\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "*.log\n")
    git(repo, "add", "logs", ".gitignore")
    git(repo, "commit", "-qm", "logs")
    (repo / "logs" / "a.log").write_text("log line\n")
    snap = take(tmp_path, repo)
    assert "logs/a.log" in json.loads((snap / "meta.json").read_text())["ignored_entries"]
    ext = tmp_path / "ext"
    ext.mkdir()

    def plant_link(clone: Path, env: dict) -> None:
        shutil.rmtree(clone / "logs")
        (clone / "logs").symlink_to(ext)

    monkeypatch.setattr(replay, "_refuse_escaping_links", plant_link)
    with pytest.raises(replay.Inconclusive, match="through a link out of the clone"):
        replay.restore(snap, tmp_path / "r")
    assert list(ext.iterdir()) == []


def test_a_path_under_a_linked_folder_is_never_left_out_through_the_link(tmp_path: Path) -> None:
    """F18: a replay replaced `logs` with a link to an outside folder. The copy
    keeps `logs` as the link it is; it never opens it up to leave `a.log` out,
    so nothing is read or removed through it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.log").write_text("SECRET=1234\n")
    (outside / "b.log").write_text("other\n")
    clone = tmp_path / "attempt" / "repo"
    clone.mkdir(parents=True)
    (clone / "logs").symlink_to(outside)
    replay._copy_selective(tmp_path / "attempt", "repo", tmp_path / "copy", {"repo/logs/a.log"})
    assert os.readlink(tmp_path / "copy" / "logs") == str(outside)
    assert sorted(p.name for p in outside.iterdir()) == ["a.log", "b.log"]


def test_a_read_only_folder_opened_up_for_an_exclusion_still_gets_its_children(tmp_path: Path, repo: Path) -> None:
    """The folder is opened up because it holds a nested checkout; its mode is
    applied only after its children are copied, or a read-only one refuses them."""
    vendor = repo / "vendor"
    (vendor / "sub").mkdir(parents=True)
    git(vendor / "sub", "init", "-q")
    (vendor / "lib.txt").write_text("lib\n")
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "vendor/\n")
    vendor.chmod(0o555)
    copied = tmp_path / "r" / "repo" / "vendor"
    try:
        clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
        assert (clone / "vendor" / "lib.txt").read_text() == "lib\n"
        assert not (clone / "vendor" / "sub").exists()
        assert stat.S_IMODE((clone / "vendor").stat().st_mode) == 0o555
    finally:
        vendor.chmod(0o755)
        if copied.exists():
            copied.chmod(0o755)


def test_a_link_that_cannot_be_resolved_is_unsafe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Python 3.12's `Path.resolve()` raises RuntimeError on a symlink loop."""
    top = tmp_path / "top"
    top.mkdir()
    (top / "x").write_text("x\n")
    (top / "l").symlink_to("x")
    assert replay._resolves_inside(top, top / "l")

    def loop(self: Path, strict: bool = False) -> Path:
        raise RuntimeError(f"Symlink loop from {self}")

    with monkeypatch.context() as m:
        m.setattr(Path, "resolve", loop)
        safe = replay._resolves_inside(top, top / "l")
    assert not safe


def test_a_symlink_loop_in_an_ignored_folder_never_breaks_restore(tmp_path: Path, repo: Path) -> None:
    (repo / "node_modules" / "a").symlink_to("b")
    (repo / "node_modules" / "b").symlink_to("a")
    clone = replay.restore(take(tmp_path, repo), tmp_path / "r")
    assert (clone / ".env").read_text() == "TOKEN=x\n"


# --- final Minor 8: git calls on the real checkout take no optional lock --------------


def test_every_git_call_on_the_real_checkout_takes_no_optional_lock(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict | None]] = []
    real_run = subprocess.run

    def recording_run(args: list[str], *rest: object, **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(([str(a) for a in args], kwargs.get("env")))  # type: ignore[arg-type]
        return real_run(args, *rest, **kwargs)  # type: ignore[call-overload]

    with monkeypatch.context() as m:
        m.setattr(subprocess, "run", recording_run)
        snap = take(tmp_path, repo)
        clone = replay.restore(snap, tmp_path / "r")
        replay.install_session(snap, clone, tmp_path / "claude-home")
    tops = {str(repo), str(repo.resolve())}
    on_source = [env for args, env in calls if args[0] == "git" and tops & set(args)]
    assert len(on_source) >= 10
    assert all(env is not None and env.get("GIT_OPTIONAL_LOCKS") == "0" for env in on_source)


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


def calls_log(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]


def prompt_of(call: dict) -> str:
    return call["args"][call["args"].index("--") + 1]


JOB_TIMEOUT = 0.5


@pytest.fixture
def job_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling T11h Minor 3: only the job call can time out, and its JOB_TIMEOUT
    clock starts once the fake has logged the call (it logs after writing its
    session file, or its pid file when it hangs at once), so a loaded machine can
    neither time out the warm-up nor kill the job before it wrote what the test
    looks for. The warm-up gets a generous limit of its own. Only `replay`'s own
    view of `subprocess` is patched, so every other command keeps the real Popen."""
    log = tmp_path / "calls.jsonl"

    def logged() -> int:
        return len(log.read_text().splitlines()) if log.exists() else 0

    class JobTimeoutPopen(subprocess.Popen):
        def __init__(self, cmd: list[str], **kwargs) -> None:
            # Reason: a warm-up sends the job's own argv; only its environment says
            # which call it is.
            self.warmup = (kwargs.get("env") or {}).get("JEV_FORK_CHECK_WARMUP") == "1"
            self.logged_before = logged()
            super().__init__(cmd, **kwargs)

        def communicate(self, input=None, timeout=None):
            if self.warmup:
                return super().communicate(input, timeout=60)
            deadline = time.monotonic() + 30
            while logged() == self.logged_before and self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            return super().communicate(input, timeout=JOB_TIMEOUT)

    monkeypatch.setattr(replay, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": JobTimeoutPopen}))


def test_install_session_rewrites_paths_and_ids(tmp_path: Path, repo: Path, snap: Path) -> None:
    clone = replay.restore(snap, tmp_path / "r")
    sid = replay.install_session(snap, clone, tmp_path / "claude-home")
    text = (replay.project_dir(tmp_path / "claude-home", clone) / f"{sid}.jsonl").read_text()
    assert str(repo.resolve()) not in text
    assert f"Edited {clone}/app.py" in text
    assert all(json.loads(line).get("sessionId", sid) == sid for line in text.splitlines())


def test_pair_runs_both_sides_and_only_delegate_gets_the_note(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"] and result["reason"] == ""
    keep, delegate = result["sides"]["keep"], result["sides"]["delegate"]
    assert keep["warm"] and delegate["warm"]
    assert delegate["delegated"] is True and keep["delegated"] is False
    assert delegate["calls"] == 2 and keep["calls"] == 1
    assert Path(keep["clone"], "RESULT.txt").read_text() == "done by keep\n"
    assert keep["skipped"] == []
    under_work = [str(Path(side["clone"]).relative_to(tmp_path / "work")) for side in (keep, delegate)]
    assert not any("keep" in p or "delegate" in p for p in under_work)
    calls = calls_log(tmp_path)
    assert all(c["router"] == "off" for c in calls)
    assert [c["note"] is not None for c in calls if not c["warmup"]] == [name == "delegate" for name in result["order"]]
    # 0.3.1 reverses Ruling T11f New 2: the warm-up gets exactly the job's note
    # (test_both_calls_of_a_side_get_the_same_note_file).
    # Ruling T11e-i: the subagent's call is counted in the delegate side's cost, not
    # just its call count.
    p = usage.load_prices()["claude-opus-5-5"]
    main_cost = 5 * p.inp + 100_000 * p.read + 200 * p.w1h + 50 * p.out
    sub_cost = 5 * p.inp + 25_000 * p.w1h + 500 * p.out
    assert keep["cost"] == pytest.approx(main_cost)
    assert delegate["cost"] == pytest.approx(main_cost + sub_cost)


def test_model_is_pinned_with_the_1m_suffix_for_a_large_context(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    """Ruling T11b: EVENT's context (803,010) is over the 1m threshold, so every
    call, warm-up and job alike, must be pinned to the snapshot's own model with
    the [1m] suffix, not whatever the replay environment's settings would pick."""
    replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    calls = calls_log(tmp_path)
    assert calls
    for c in calls:
        assert "--model" in c["args"]
        assert c["args"][c["args"].index("--model") + 1] == "claude-opus-5-5[1m]"


def test_missing_model_makes_the_pair_inconclusive_without_running_anything(
    tmp_path: Path, repo: Path, fake_claude: Path
) -> None:
    event = {k: v for k, v in EVENT.items() if k != "model"}
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", event)
    snap = tmp_path / "fc" / "snapshots" / sid
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "no model recorded"
    assert result["sides"] == {}
    assert not (tmp_path / "calls.jsonl").exists()


def test_crashed_job_is_retried_then_inconclusive(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_CRASH", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "crashed"
    # Ruling T11e-h: once the first side is inconclusive, the second never runs.
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == replay.ATTEMPTS


def test_dirty_warmup_discards_the_attempt_and_is_retried(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_DIRTY_WARMUP", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "warm-up changed the clone"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == replay.ATTEMPTS


def test_cold_warmup_never_runs_the_job(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_COLD_WARMUP", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "never warm"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == replay.ATTEMPTS
    calls = calls_log(tmp_path)
    assert calls and all(c["warmup"] for c in calls)


def test_cold_attempt_is_retried_then_the_next_attempt_is_scored(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_COLD_WARMUP_ONCE", str(tmp_path / "warmup_seen"))
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    first, second = result["order"]
    assert result["sides"][first]["attempt"] == 2
    assert result["sides"][second]["attempt"] == 1


def test_unpriced_call_makes_the_pair_inconclusive(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_UNPRICED", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "unpriced"
    # Ruling T11f Open 2: an unpriced call stops the pair at once; the second side
    # never runs.
    assert len(result["sides"]) == 1


def test_unpriced_warmup_stops_at_once_instead_of_being_retried_as_never_warm(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11g small 3: an unpriced call happened (unlike a truly cold, calls
    == 0 warm-up), so a fresh clone would not fix it; it must not be retried."""
    monkeypatch.setenv("FAKE_CLAUDE_UNPRICED_WARMUP", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "unpriced"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == 1


def test_warm_warmup_but_cold_job_is_retried(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11f New 1: the warm-up's own calls being priced does not prove the
    job's own first call actually read the shared prefix from cache; both checks
    must run."""
    monkeypatch.setenv("FAKE_CLAUDE_COLD_JOB", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "never warm"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == replay.ATTEMPTS


def test_model_has_no_suffix_for_a_context_at_or_below_the_threshold(
    tmp_path: Path, repo: Path, fake_claude: Path
) -> None:
    event = {**EVENT, "context": 200_000}
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", event)
    snap = tmp_path / "fc" / "snapshots" / sid
    replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    calls = calls_log(tmp_path)
    assert calls
    for c in calls:
        assert c["args"][c["args"].index("--model") + 1] == "claude-opus-5-5"


def test_timeout_makes_the_pair_inconclusive_at_once_without_retrying(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replay, "TIMEOUT_SECONDS", 0.01)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_CHILD", str(tmp_path / "child.pid"))
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "timed out"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    # Ruling T11f Also: a timeout is never retried.
    assert result["sides"][first]["attempt"] == 1


def test_timeout_kills_the_whole_process_group(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, job_times_out: None
) -> None:
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_CHILD", str(pidfile))
    # Ruling T11g small 1: -1 is also SIGHUP's own exit code, so the timeout is its
    # own flag now, never inferred from the exit code alone.
    _, _, _, timed_out = replay.run_claude(tmp_path, "sid", "hello", {}, fake_claude, "claude-opus-5-5")
    assert timed_out is True
    child_pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the sleep child was not killed along with the timed-out fake claude")


def test_job_timeout_gives_the_reason_timed_out(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, job_times_out: None
) -> None:
    """Ruling T11g small 5: the warm-up succeeds normally; only the job times out."""
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_CHILD_JOB", str(tmp_path / "child.pid"))
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "timed out"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == 1
    # Ruling T11h Minor 3: the warm-up finished and the job was the call that hung,
    # so the reason comes from the job's own timeout branch, not the warm-up's.
    calls = calls_log(tmp_path)
    assert [(c["warmup"], c.get("hung", False)) for c in calls] == [(True, False), (False, True)]


def test_timeout_after_a_leak_reports_the_leak_not_timed_out(
    tmp_path: Path,
    snap: Path,
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    job_times_out: None,
) -> None:
    """Ruling T11g Open 1b: the fake writes a session file whose new turn names the
    real toplevel, then hangs past its timeout. Found via directory listing
    (Open 1b), not stdout, which a hung call never prints. The reason is the leak,
    not the timeout, and cmd_replay prints the warning."""
    meta = json.loads((snap / "meta.json").read_text())
    monkeypatch.setenv("FAKE_CLAUDE_LEAK_PATH", meta["toplevel"])
    monkeypatch.setenv("FAKE_CLAUDE_HANG_AFTER_WRITE", "1")
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    assert fork_check.main(["replay", snap.name]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "real repository" in out
    status = fork_check.statuses()[snap.name]
    assert status["status"] == "inconclusive"
    assert status["reason"] == "a replay used a path into the real repository"
    # Ruling T11h Minor 3: the warm-up finished, then the job ran (and hung).
    assert [c["warmup"] for c in calls_log(tmp_path)] == [True, False]
    # Ruling T11h: cmd_replay found its Claude Code home through CLAUDE_CONFIG_DIR,
    # which conftest points into tmp_path, never the user's real ~/.claude.
    assert list((tmp_path / "claude-config" / "projects").glob("*/*.jsonl"))


def test_leaked_real_path_makes_the_pair_inconclusive_and_stops_the_second_side(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = json.loads((snap / "meta.json").read_text())
    monkeypatch.setenv("FAKE_CLAUDE_LEAK_PATH", meta["toplevel"])
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"]
    assert result["reason"] == "a replay used a path into the real repository"
    assert len(result["sides"]) == 1


def test_replay_pair_never_touches_the_real_repository(
    tmp_path: Path, repo: Path, snap: Path, fake_claude: Path
) -> None:
    before_status = git(repo, "status", "--porcelain")
    before_head = git(repo, "rev-parse", "HEAD").strip()
    replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert git(repo, "status", "--porcelain") == before_status
    assert git(repo, "rev-parse", "HEAD").strip() == before_head


def test_prompt_starting_with_a_dash_is_never_parsed_as_an_option(
    tmp_path: Path, snap: Path, fake_claude: Path
) -> None:
    (snap / "message.txt").write_text("- do the thing")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    keep = result["sides"]["keep"]
    assert Path(keep["clone"], "RESULT.txt").read_text() == "done by keep\n"


def test_keep_side_never_inherits_a_stray_note_file_env_var(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JEV_ROUTER_NOTE_FILE", "/should/not/leak")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    calls = calls_log(tmp_path)
    assert all(c["note"] != "/should/not/leak" for c in calls)
    assert any(c["note"] == str(snap / "note.txt") for c in calls)


SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_EFFORT",
    "CLAUDE_PID",
)


def test_replays_never_inherit_the_launching_sessions_markers(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner usually runs inside a Claude Code session; its markers must not
    tie either replay to that session. Everything else is kept, including the
    auth and provider variables (A6, narrowed)."""
    for name in SESSION_MARKERS:
        monkeypatch.setenv(name, "from-the-launching-session")
    for name in replay.KEPT_CLAUDE_CODE_VARS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("CLAUDE_TEST_KEPT", "yes")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    calls = calls_log(tmp_path)
    assert len(calls) == 4
    assert replay.KEPT_CLAUDE_CODE_VARS == {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "CLAUDE_CODE_SKIP_VERTEX_AUTH",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        "CLAUDE_CODE_CLIENT_CERT",
        "CLAUDE_CODE_CLIENT_KEY",
        "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    }
    for call in calls:
        inherited = set(call["claude_env"])
        assert not [
            k
            for k in inherited
            if k in SESSION_MARKERS or (k.startswith("CLAUDE_CODE_") and k not in replay.KEPT_CLAUDE_CODE_VARS)
        ]
        assert {"CLAUDE_CONFIG_DIR", "CLAUDE_TEST_KEPT", *replay.KEPT_CLAUDE_CODE_VARS} <= inherited
        assert (call["home"], call["path"]) == (os.environ["HOME"], os.environ["PATH"])


def test_install_session_refuses_a_transcript_line_that_does_not_parse(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    lines = transcript.read_text().splitlines()
    lines[0] = "not json at all"  # corrupt the earliest line; the latest stays a cut point
    transcript.write_text("\n".join(lines) + "\n")
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    # Ruling T11f Also: the wording covers a line that never parsed at all, not just
    # one the rewrite itself broke.
    with pytest.raises(replay.Inconclusive, match="did not parse as JSON"):
        replay.install_session(snap, clone, tmp_path / "claude-home")


def test_install_session_refuses_a_copy_that_still_names_the_real_path(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11f Open 3: right after every line is rewritten, the installed copy
    is scanned once more, before any `claude` call spends real time or money."""
    clone = replay.restore(snap, tmp_path / "r")
    monkeypatch.setattr(replay, "_rewrite_real_paths", lambda text, tops, clone: text)
    with pytest.raises(replay.Inconclusive, match="still names the real repository"):
        replay.install_session(snap, clone, tmp_path / "claude-home")
    target = replay.project_dir(tmp_path / "claude-home", clone)
    assert not list(target.glob("*.jsonl")) if target.exists() else True


def test_path_boundary_rewrites_shell_quoted_and_composed_forms(tmp_path: Path, repo: Path) -> None:
    """Ruling T11f Open 1: the boundary regressed to only `/`, a quote, whitespace,
    a backslash or end of string, so `cd '<top>'`, `cd <top>;`, `(cd <top>)`,
    `` `<top>` `` and `PYTHONPATH=<top>:x` all kept the real path. One boundary,
    `(?![\\w.-])`, must cover all five, and the sibling test must still pass."""
    real_repo = repo.resolve()
    other = real_repo.parent / (real_repo.name + "-other")
    forms = [
        f"cd '{real_repo}' && ls",
        f"cd {real_repo}; ls",
        f"(cd {real_repo})",
        f"`{real_repo}`",
        f"PYTHONPATH={real_repo}:x",
    ]
    text = "; ".join(forms) + f"; keep {other}/keep.py"
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text=text)])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap2 = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap2, tmp_path / "r")
    new_sid = replay.install_session(snap2, clone, tmp_path / "claude-home")
    rewritten = (replay.project_dir(tmp_path / "claude-home", clone) / f"{new_sid}.jsonl").read_text()
    for original in forms:
        assert original not in rewritten
        assert original.replace(str(real_repo), str(clone)) in rewritten
    assert f"{other}/keep.py" in rewritten


def test_leak_scan_runs_on_a_crashed_attempt_too(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11f Open 1: a leak must be caught even when the job that produced it
    also reported is_error (a graceful crash: the session file is written, with the
    leaking tool call in it, before the CLI reports the error), not only after a
    clean success."""
    meta = json.loads((snap / "meta.json").read_text())
    monkeypatch.setenv("FAKE_CLAUDE_LEAK_PATH", meta["toplevel"])
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_RESULT", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"]
    assert result["reason"] == "a replay used a path into the real repository"


@pytest.mark.parametrize("call", ["warmup", "job"])
def test_leak_is_scanned_before_the_reported_session_id_is_checked(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """Ruling T11h Minor 2: a call whose JSON names a session it never wrote is
    still scanned first, so its leak is what the pair reports, on the warm-up
    and the job alike."""
    meta = json.loads((snap / "meta.json").read_text())
    monkeypatch.setenv("FAKE_CLAUDE_WRONG_SESSION_ID", call)
    leak_var = "FAKE_CLAUDE_LEAK_PATH_WARMUP" if call == "warmup" else "FAKE_CLAUDE_LEAK_PATH"
    monkeypatch.setenv(leak_var, meta["toplevel"])
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"]
    assert result["reason"] == "a replay used a path into the real repository"


@pytest.mark.parametrize("call", ["warmup", "job"])
def test_a_reported_session_id_that_was_never_written_is_an_error(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """With nothing leaked, the id check still runs after the scan and still
    refuses a session id the directory listing never saw."""
    monkeypatch.setenv("FAKE_CLAUDE_WRONG_SESSION_ID", call)
    with pytest.raises(RuntimeError, match="not among the session files"):
        replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))


def test_job_prompt_is_rewritten_before_it_reaches_claude(tmp_path: Path, repo: Path, fake_claude: Path) -> None:
    """Ruling T11g New (Important): a message naming the real checkout (e.g. "fix
    ~/work/x/y.py") must never reach `claude` verbatim under bypass permissions."""
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    message = f"fix {repo}/x.py, please"
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), message, "NOTE", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    calls = calls_log(tmp_path)
    # Reason: since 0.3.1 the warm-up sends the job's own (rewritten) message too.
    assert [c["warmup"] for c in calls] == [True, False, True, False]
    for c in calls:
        assert str(repo) not in prompt_of(c)
        assert prompt_of(c).endswith("/x.py, please")


def test_refuse_if_leaked_matches_a_non_ascii_path() -> None:
    """Ruling T11g small 2: `_refuse_if_leaked`'s own regex match must see a
    non-ASCII path; the fix is in `install_session`'s ensure_ascii=False, exercised
    end to end by the next test."""
    forms = ["/Users/x/wörk/agent-skills"]
    text = json.dumps({"note": "see /Users/x/wörk/agent-skills/CLAUDE.md"}, ensure_ascii=False)
    with pytest.raises(replay.Inconclusive, match="still names the real repository"):
        replay._refuse_if_leaked(text, forms)


def test_install_session_refuses_a_non_ascii_real_path_behind_an_escape(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11g small 2, end to end: the transcript line holds the real path
    only as `w\\u00f6rk` (how `json.dumps` writes it by default). The refuse check
    reads the decoded text, so the accented path is still found there even though
    the escaped line never contains it literally."""
    real = "/Users/x/wörk/agent-skills"
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text=f"see {real}/CLAUDE.md")])
    assert real not in transcript.read_text()
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    monkeypatch.setattr(replay, "_worktree_paths", lambda meta: [real])
    with pytest.raises(replay.Inconclusive, match="still names the real repository"):
        replay.install_session(snap, clone, tmp_path / "claude-home")


def test_install_session_keeps_a_lone_surrogate_from_a_cut_emoji(tmp_path: Path, repo: Path) -> None:
    """Ruling T11h Minor 1: Claude Code can store a lone UTF-16 surrogate (`\\ud83d`)
    when it cuts text mid-emoji. UTF-8 cannot encode one, so the installed copy is
    written with JSON's own escaping, and reads back to the very same text."""
    cut = "cut mid-emoji \ud83d"
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text=cut)])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    clone = replay.restore(snap, tmp_path / "r")
    new_sid = replay.install_session(snap, clone, tmp_path / "claude-home")
    text = (replay.project_dir(tmp_path / "claude-home", clone) / f"{new_sid}.jsonl").read_text()
    entries = [json.loads(line) for line in text.splitlines()]
    assert [e["message"]["content"][0]["text"] for e in entries if e["type"] == "assistant"] == [cut]


def test_history_naming_a_sibling_checkout_is_not_flagged(tmp_path: Path, repo: Path, fake_claude: Path) -> None:
    """Ruling T11f Open 3: a sibling checkout's name (`<repo>-sibling`) shares a
    literal text prefix with the real checkout's own path, and a leak scan that
    read the whole forked file (inherited history included) would risk flagging
    it; the new-turn-scoped scan never even looks at that history."""
    sibling = f"{repo}-sibling"
    entries = [
        typed("earlier"),
        {
            "type": "assistant",
            "requestId": "r0",
            "message": {
                "model": "claude-opus-5-5",
                "content": [
                    {"type": "tool_use", "id": "r0", "name": "Read", "input": {"file_path": f"{sibling}/README.md"}}
                ],
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 50,
                    "output_tokens": 10,
                },
            },
        },
        typed("start"),
        assistant("r1", text=f"Edited {repo}/app.py"),
    ]
    transcript = write(tmp_path / "t.jsonl", entries)
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "NOTE", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]


# --- 0.3.1: the warm-up is launched exactly like its job, and a guard stops it -------------

WARMUP_HOOK = SCRIPTS / "warmup_hook.py"
# The hook's exact output in a warm-up, kept verbatim so a reworded one is caught too.
STOP_TEXT = (
    '{"continue": false, "stopReason": "fork-check warm-up", "hookSpecificOutput": '
    '{"hookEventName": "PreToolUse", "permissionDecision": "deny", '
    '"permissionDecisionReason": "fork-check warm-up: no tools"}}'
)


def warmups_and_jobs(calls: list[dict]) -> list[tuple[dict, dict]]:
    """The calls log as (warm-up, job) pairs, in the order they ran."""
    assert [c["warmup"] for c in calls] == [True, False] * (len(calls) // 2)
    return list(zip(calls[::2], calls[1::2], strict=True))


def test_warmup_and_job_are_launched_identically(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    """A prompt-cache entry ends with the exact request that wrote it, so the
    runner launches the warm-up exactly like the job: the same message,
    `--settings`, model and flags, from the same folder, with the same
    environment but for JEV_FORK_CHECK_WARMUP, whose hook stops the warm-up
    before its tool runs. That makes the invocations identical, not the API
    requests (runtime state can still differ); warmth is checked per attempt."""
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"], result["reason"]
    pairs = warmups_and_jobs(calls_log(tmp_path))
    assert len(pairs) == 2
    for warm, job in pairs:
        assert warm["argv"] == job["argv"]
        assert warm["cwd"] == job["cwd"]
        assert "--settings" in job["args"]
        assert warm["env_digest"] == job["env_digest"]
        assert (warm["warmup_var"], job["warmup_var"]) == ("1", None)
        assert (warm["hook_stopped"], job["hook_stopped"]) == (True, False)
        assert (warm["tool_ran"], job["tool_ran"]) == (False, True)
    # Reason: the two sides resume different session copies, but every call
    # carries the very same settings string.
    settings = {c["args"][c["args"].index("--settings") + 1] for pair in pairs for c in pair}
    assert len(settings) == 1


def test_both_calls_of_a_side_get_the_same_note_file(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    """The jev-router hook adds the note to the first request, so the warm-up must
    get exactly the job's note (this reverses Ruling T11f New 2): on the
    delegate side both calls get the snapshot's note file, on the keep side
    neither has one. The hook still stops the delegate warm-up before it can
    hand anything to the helper."""
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"], result["reason"]
    pairs = warmups_and_jobs(calls_log(tmp_path))
    note = {"delegate": str(snap / "note.txt"), "keep": None}
    assert [(warm["note"], job["note"]) for warm, job in pairs] == [(note[n], note[n]) for n in result["order"]]
    assert result["sides"]["delegate"]["delegated"] is True
    assert Path(result["sides"]["delegate"]["clone"], "RESULT.txt").read_text() == "done by delegate\n"


def test_an_inherited_warmup_variable_never_reaches_the_job(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JEV_FORK_CHECK_WARMUP=1 left in the runner's own environment must never
    turn a job into a warm-up: the job's environment removes the variable."""
    monkeypatch.setenv("JEV_FORK_CHECK_WARMUP", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"], result["reason"]
    pairs = warmups_and_jobs(calls_log(tmp_path))
    assert [(warm["warmup_var"], job["warmup_var"]) for warm, job in pairs] == [("1", None), ("1", None)]
    for name in ("keep", "delegate"):
        assert Path(result["sides"][name]["clone"], "RESULT.txt").read_text() == f"done by {name}\n"


def run_warmup_hook(value: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "JEV_FORK_CHECK_WARMUP"}
    if value is not None:
        env["JEV_FORK_CHECK_WARMUP"] = value
    hook_input = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    return subprocess.run([sys.executable, str(WARMUP_HOOK)], input=hook_input, env=env, capture_output=True, text=True)


def test_warmup_hook_stops_and_denies_during_a_warmup() -> None:
    done = run_warmup_hook("1")
    assert done.returncode == 0
    assert json.loads(done.stdout) == json.loads(STOP_TEXT)
    assert done.stdout.strip() == STOP_TEXT


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_warmup_hook_prints_nothing_outside_a_warmup(value: str | None) -> None:
    done = run_warmup_hook(value)
    assert (done.returncode, done.stdout) == (0, "")


def test_settings_run_the_warmup_hook_by_absolute_path_with_this_interpreter(
    tmp_path: Path, snap: Path, fake_claude: Path
) -> None:
    """The `--settings` every call gets parses as JSON and holds one PreToolUse
    hook for every tool, whose command runs warmup_hook.py by absolute path with
    the runner's own interpreter, so it never depends on the user's login shell
    (theirs is fish) or PATH. Ruling R1: `"onFailure": "block"`, since Claude
    Code otherwise lets the tool run when a hook fails. Run through /bin/sh, it
    stops a warm-up."""
    replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    args = calls_log(tmp_path)[0]["args"]
    settings = json.loads(args[args.index("--settings") + 1])
    assert list(settings) == ["hooks"] and list(settings["hooks"]) == ["PreToolUse"]
    (group,) = settings["hooks"]["PreToolUse"]
    assert group["matcher"] == "*"
    (hook,) = group["hooks"]
    assert hook["type"] == "command"
    assert hook["onFailure"] == "block"
    command = shlex.split(hook["command"])
    assert command == [sys.executable, str(WARMUP_HOOK)]
    assert Path(command[1]).is_absolute() and Path(command[1]).name == "warmup_hook.py"
    env = {**os.environ, "JEV_FORK_CHECK_WARMUP": "1"}
    done = subprocess.run(["/bin/sh", "-c", hook["command"]], env=env, capture_output=True, text=True)
    assert json.loads(done.stdout) == json.loads(STOP_TEXT)


HOOK_COMMAND = f"{shlex.quote(sys.executable)} {shlex.quote(str(WARMUP_HOOK))}"


def settings_running(command: str, *, on_failure: str | None = "block") -> str:
    """A `--settings` string shaped like replay.SETTINGS, running `command`."""
    hook: dict = {"type": "command", "command": command}
    if on_failure is not None:
        hook["onFailure"] = on_failure
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [hook]}]}})


def test_the_real_warmup_guard_passes_its_preflight() -> None:
    assert replay.warmup_guard_passes_preflight()


@pytest.mark.parametrize("broken", ["missing", "always-stops", "never-stops", "stops-without-denying"])
def test_a_warmup_guard_that_fails_its_preflight_runs_nothing(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """Ruling R1: before any `claude` runs, the pair runs the exact hook command
    from SETTINGS with JEV_FORK_CHECK_WARMUP=1 (it must print the stop-and-deny
    decision) and without it (it must print nothing and exit 0). A guard that
    cannot start, would stop the job too, or would let a warm-up's tool run
    makes the pair inconclusive, and nothing else runs: no clone, no call."""
    command = {
        "missing": f"{shlex.quote(sys.executable)} {shlex.quote(str(tmp_path / 'gone.py'))}",
        "always-stops": f"printf '%s' {shlex.quote(STOP_TEXT)}",
        "never-stops": "true",
        "stops-without-denying": """if [ "$JEV_FORK_CHECK_WARMUP" = 1 ]; then printf '%s' '{"continue": false}'; fi""",
    }[broken]
    monkeypatch.setattr(replay, "SETTINGS", settings_running(command))
    assert not replay.warmup_guard_passes_preflight()
    # Reason: not `work`, which the repo fixture's own checkout lives under.
    result = replay.replay_pair(snap, tmp_path / "replays", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "warm-up guard failed its preflight"
    assert result["sides"] == {}
    assert not (tmp_path / "replays").exists()
    assert not (tmp_path / "calls.jsonl").exists()


def test_a_warmup_that_ran_a_tool_ends_the_pair_at_once(
    tmp_path: Path,
    snap: Path,
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ruling R1: when the guard fails at run time (here it cannot start once the
    preflight has passed) and nothing makes the failure block, the warm-up runs
    the job's own tool under bypass permissions, and its transcript shows it. The
    side and the pair end at once, with no retry (an effect may already have
    happened, and a fresh clone does not mend the guard), and the runner warns."""
    monkeypatch.setattr(replay, "SETTINGS", settings_running(HOOK_COMMAND, on_failure=None))
    monkeypatch.setenv("FAKE_CLAUDE_HOOK_FAILS", "warmup")
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    assert fork_check.main(["replay", snap.name]) == 0
    status = fork_check.statuses()[snap.name]
    assert (status["status"], status["reason"]) == ("inconclusive", "warm-up ran a tool")
    out = capsys.readouterr().out
    assert "WARNING" in out and "warm-up ran a tool" in out
    # Reason: one warm-up, whose tool ran; no job, no retry, no second side.
    assert [(c["warmup"], c["tool_ran"]) for c in calls_log(tmp_path)] == [(True, True)]


def test_a_guard_that_fails_at_run_time_still_blocks_the_warmups_tool(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the real SETTINGS (`"onFailure": "block"`), a hook that cannot start
    blocks the tool instead of letting it run: the warm-up changes nothing, and
    its blocked call is a hook's rejection, not a tool that ran."""
    monkeypatch.setenv("FAKE_CLAUDE_HOOK_FAILS", "warmup")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"], result["reason"]
    pairs = warmups_and_jobs(calls_log(tmp_path))
    assert [(warm["tool_ran"], job["tool_ran"]) for warm, job in pairs] == [(False, True), (False, True)]


# Reason: these two mirror what Claude Code 2.1.295 really recorded in the trial clone:
# the guard's denial (warmup_design2_experiment.py's warm-up) and a Bash call that ran
# (its job), cut down to the fields that matter.
DENIED_BY_THE_GUARD = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "content": "PreToolUse:Bash hook error: fork-check warm-up: no tools",
                "is_error": True,
                "tool_use_id": "toolu_1",
            }
        ],
    },
    "toolUseResult": "Error: PreToolUse:Bash hook error: fork-check warm-up: no tools",
    "toolDenialKind": "permission-rule",
    "permissionDecision": {"decision": "reject", "source": "hook", "reasonType": "hook"},
}
RAN = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {
                "tool_use_id": "toolu_1",
                "type": "tool_result",
                "content": "(Bash completed with no output)",
                "is_error": False,
            }
        ],
    },
    "toolUseResult": {"stdout": "", "stderr": "", "interrupted": False, "isImage": False},
    "permissionDecision": {"decision": "accept", "source": "config", "reasonType": "mode"},
}
NOT_AN_ERROR = {
    **DENIED_BY_THE_GUARD,
    "message": {"role": "user", "content": [{**DENIED_BY_THE_GUARD["message"]["content"][0], "is_error": False}]},
}
NO_DECISION = {k: v for k, v in DENIED_BY_THE_GUARD.items() if k != "permissionDecision"}
REJECTED_ELSEWHERE = {**DENIED_BY_THE_GUARD, "permissionDecision": {"decision": "reject", "source": "config"}}
# Reason: a second call, as the real side-2 warm-up of the 0.3.1 trial made in parallel
# with its first (both were denied, each with its own result).
SECOND_TOOL_USE = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {}}]},
}
SECOND_DENIED = {
    **DENIED_BY_THE_GUARD,
    "message": {
        "role": "user",
        "content": [{**DENIED_BY_THE_GUARD["message"]["content"][0], "tool_use_id": "toolu_2"}],
    },
}


@pytest.mark.parametrize(
    ("history", "turn", "subagent", "ran"),
    [
        ([], [DENIED_BY_THE_GUARD], [], False),
        ([], [RAN], [], True),
        ([], [DENIED_BY_THE_GUARD, RAN], [], True),
        ([], [NOT_AN_ERROR], [], True),
        ([], [NO_DECISION], [], True),
        ([], [REJECTED_ELSEWHERE], [], True),
        ([RAN], [DENIED_BY_THE_GUARD], [], False),
        ([], [DENIED_BY_THE_GUARD], [RAN], True),
        ([], [], [], True),
        ([], [SECOND_TOOL_USE, DENIED_BY_THE_GUARD, SECOND_DENIED], [], False),
        ([], [SECOND_TOOL_USE, DENIED_BY_THE_GUARD], [], True),
        ([], [SECOND_DENIED], [], True),
        ([], [DENIED_BY_THE_GUARD], [SECOND_TOOL_USE], True),
    ],
    ids=[
        "denied",
        "ran",
        "denied-then-ran",
        "not-an-error",
        "no-decision",
        "rejected-elsewhere",
        "ran-only-in-history",
        "ran-in-a-subagent",
        "call-without-result",
        "parallel-calls-all-denied",
        "second-call-without-result",
        "result-for-another-call",
        "subagent-call-without-result",
    ],
)
def test_ran_a_tool_reads_the_new_turn_and_its_subagents(
    tmp_path: Path, history: list[dict], turn: list[dict], subagent: list[dict], ran: bool
) -> None:
    """Ruling R1: any tool_result in the warm-up's own new turn, or in its
    subagents' transcripts, that is not a rejection by a hook counts as a tool
    that ran; anything that does not look exactly like one fails closed. Ruling
    R7: so does any tool call there without a hook-rejected result of its own
    (Claude Code may have crashed after the tool ran, before recording it). The
    inherited history, where the real session's tools ran, never counts."""
    session = write(tmp_path / "s.jsonl", [*history, typed("do it"), TOOL_USE, *turn])
    if subagent:
        (tmp_path / "s" / "subagents").mkdir(parents=True)
        write(tmp_path / "s" / "subagents" / "agent-1.jsonl", subagent)
    assert replay._ran_a_tool([session], "do it") is ran


TOOL_USE = {
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]},
}


def test_ran_a_tool_fails_closed_when_the_turn_cannot_be_found(tmp_path: Path) -> None:
    """Ruling R4: a session file with entries but none carrying the prompt cannot
    show that no tool ran, so it counts as one that did. An empty file, where
    nothing was recorded at all, does not."""
    session = write(tmp_path / "s.jsonl", [typed("recorded some other way"), TOOL_USE, DENIED_BY_THE_GUARD])
    assert replay._ran_a_tool([session], "do it") is True
    assert replay._ran_a_tool([write(tmp_path / "empty.jsonl", [])], "do it") is False


def test_a_warmup_whose_turn_cannot_be_found_ends_the_pair(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R4, end to end: a warm-up transcript with a tool_result but no entry
    matching the prompt ends the pair at once as "warm-up ran a tool", instead
    of being retried as never warm."""
    monkeypatch.setenv("FAKE_CLAUDE_PROMPT_RECORDED_AS", "the message, recorded some other way")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "warm-up ran a tool"
    assert [c["warmup"] for c in calls_log(tmp_path)] == [True]


def guard_failure(command: str) -> str:
    """A blocked tool_result's text as Claude Code 2.1.295 words a hook failure
    under `"onFailure": "block"`."""
    return f'PreToolUse:Bash hook error: [{command}]: failed; blocking because onFailure is "block"\nNo stderr output'


def rejected(text: str) -> dict:
    return {
        **DENIED_BY_THE_GUARD,
        "message": {"role": "user", "content": [{**DENIED_BY_THE_GUARD["message"]["content"][0], "content": text}]},
    }


@pytest.mark.parametrize(
    ("turn", "subagent", "blocked"),
    [
        ([rejected(guard_failure(HOOK_COMMAND))], [], True),
        ([], [rejected(guard_failure(HOOK_COMMAND))], True),
        ([rejected(guard_failure("sh -c 'exit 1'"))], [], False),
        ([rejected("PreToolUse:Bash hook error: the user's own policy hook denies this tool")], [], False),
        ([{**rejected(guard_failure(HOOK_COMMAND)), "permissionDecision": RAN["permissionDecision"]}], [], False),
        ([RAN], [], False),
    ],
    ids=["ours", "ours-in-a-subagent", "another-hook-failed", "another-hook-denied", "not-a-rejection", "ran"],
)
def test_guard_blocked_a_tool_names_only_our_own_guard(
    tmp_path: Path, turn: list[dict], subagent: list[dict], blocked: bool
) -> None:
    """Ruling R5: in a job, a hook rejection whose text names `[<command>]` for a
    hook command in SETTINGS can only be our guard failing (it never denies in a
    job). Another hook's rejection, the user's own, is a real job's business."""
    session = write(tmp_path / "s.jsonl", [typed("do it"), TOOL_USE, *turn])
    if subagent:
        (tmp_path / "s" / "subagents").mkdir(parents=True)
        write(tmp_path / "s" / "subagents" / "agent-1.jsonl", subagent)
    assert replay._guard_blocked_a_tool([session], "do it") is blocked


def test_a_job_tool_blocked_by_a_failing_guard_is_retried_then_inconclusive(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R5: if the guard fails during the job, `"onFailure": "block"` rejects
    the job's own tools, and the job must not be scored as if it were real. The
    attempt is retried like a cold one, in a fresh clone; when every attempt
    shows it, the side (and the pair) ends inconclusive with that reason."""
    monkeypatch.setenv("FAKE_CLAUDE_HOOK_FAILS", "job")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "warm-up guard blocked a job tool"
    first = result["order"][0]
    assert list(result["sides"]) == [first]
    assert result["sides"][first]["attempt"] == replay.ATTEMPTS
    calls = calls_log(tmp_path)
    assert [(c["warmup"], c["tool_ran"]) for c in calls] == [(True, False), (False, False)] * replay.ATTEMPTS


def test_a_job_tool_denied_by_another_hook_is_scored_normally(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R5: the user's own hooks legitimately deny tools in real jobs; such a
    rejection is part of the job and never makes it inconclusive."""
    monkeypatch.setenv("FAKE_CLAUDE_OTHER_HOOK_DENIES", "job")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"], result["reason"]
    assert all(side["warm"] and side["attempt"] == 1 for side in result["sides"].values())
    assert [c["tool_ran"] for c in calls_log(tmp_path) if not c["warmup"]] == [False, False]


def test_a_warmup_tool_call_with_no_recorded_result_ends_the_pair(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R7, end to end: with the guard skipped, Claude Code crashes after the
    warm-up's tool ran but before it recorded the result. The bare tool call
    counts as a tool that ran, so the pair ends at once instead of the crash
    being retried (which would run the tool again)."""
    monkeypatch.setattr(replay, "SETTINGS", settings_running(HOOK_COMMAND, on_failure=None))
    monkeypatch.setenv("FAKE_CLAUDE_HOOK_FAILS", "warmup")
    monkeypatch.setenv("FAKE_CLAUDE_CRASH_BEFORE_RESULT", "warmup")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"] and result["reason"] == "warm-up ran a tool"
    assert [(c["warmup"], c["tool_ran"]) for c in calls_log(tmp_path)] == [(True, True)]


@pytest.mark.parametrize(("extra", "scored"), [(20_000, False), (1_000, True)])
def test_a_job_must_read_its_own_first_request_from_cache(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, extra: int, scored: bool
) -> None:
    """Ruling R8: reading the warm-up's whole context is not enough when the job's
    own first request is longer (an attachment regenerated with more content):
    the uncached rest is paid for, and it can differ between the sides. The
    job's first read must also cover its own first context (within the same
    bound), or the attempt is never warm; a job that matches is scored."""
    monkeypatch.setenv("FAKE_CLAUDE_JOB_EXTRA_UNCACHED", str(extra))
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    if scored:
        assert not result["inconclusive"], result["reason"]
        assert all(side["attempt"] == 1 for side in result["sides"].values())
    else:
        assert result["inconclusive"] and result["reason"] == "never warm"
        first = result["order"][0]
        assert list(result["sides"]) == [first]
        assert result["sides"][first]["attempt"] == replay.ATTEMPTS


def _repo_state(repo: Path) -> tuple[str, str, str, bytes]:
    """A repository's remotes, refs, status and index bytes, read as itself."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def show(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], env=env, check=True, capture_output=True, text=True
        ).stdout

    return show("remote", "-v"), show("for-each-ref"), show("status", "--porcelain"), (repo / ".git/index").read_bytes()


def test_inherited_repository_variables_never_reach_another_repository(
    tmp_path: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling R6: git obeys GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the other
    repository-local variables over `-C`, so a runner started with them set
    would act on the repository they name (`remote remove origin` would drop its
    remote), and so would the replayed claude's own git commands. Every git call
    of a replay, and claude's environment, go without them."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    git(real, "init", "-q", "-b", "main")
    (real / "f.txt").write_text("x\n")
    git(real, "add", ".")
    git(real, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init")
    git(real, "remote", "add", "origin", "https://example.com/elsewhere.git")
    before = _repo_state(real)
    outcome: dict | Exception
    with monkeypatch.context() as m:
        m.setenv("GIT_DIR", str(real / ".git"))
        m.setenv("GIT_WORK_TREE", str(real))
        m.setenv("GIT_INDEX_FILE", str(real / ".git" / "index"))
        try:
            outcome = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
        except Exception as exc:  # Reason: the repository is checked whatever happened.
            outcome = exc
    assert _repo_state(real) == before
    assert isinstance(outcome, dict) and not outcome["inconclusive"], outcome
    local = replay._local_git_vars()
    assert {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"} <= local
    calls = calls_log(tmp_path)
    assert calls and all(not set(c["git_env"]) & local for c in calls)


def test_install_session_rewrites_the_main_worktree_and_home_forms_but_not_a_similar_path(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T11a: a session from a linked worktree also names the main checkout
    (e.g. reading a symlinked CLAUDE.md), sometimes as `~/...`. Both must be rewritten
    to the clone, and a merely similarly-named path must be left untouched."""
    wt = repo / ".claude" / "worktrees" / "x"
    git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    real_repo, real_wt = repo.resolve(), wt.resolve()
    # Reason: this must NOT be rewritten, even though it shares real_repo's own
    # path as a literal text prefix.
    other = real_repo.parent / (real_repo.name + "-other")
    other.mkdir(parents=True, exist_ok=True)
    home = real_repo.parent
    tilde_repo = "~/" + real_repo.name
    transcript = write(
        tmp_path / "t.jsonl",
        [
            typed("start"),
            assistant(
                "r1",
                text=(
                    f"Edited {real_wt}/app.py; see {real_repo}/CLAUDE.md; "
                    f"also {tilde_repo}/tilde.py; keep {other}/keep.py"
                ),
            ),
        ],
    )
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(wt, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    monkeypatch.setattr(Path, "home", lambda: home)
    clone = replay.restore(snap, tmp_path / "r")
    new_sid = replay.install_session(snap, clone, tmp_path / "claude-home")
    text = (replay.project_dir(tmp_path / "claude-home", clone) / f"{new_sid}.jsonl").read_text()
    assert f"{real_wt}/app.py" not in text
    assert f"{real_repo}/CLAUDE.md" not in text
    assert f"{tilde_repo}/tilde.py" not in text
    assert f"Edited {clone}/app.py" in text
    assert f"see {clone}/CLAUDE.md" in text
    assert f"also {clone}/tilde.py" in text
    assert f"{other}/keep.py" in text


def test_cmd_replay_marks_safe_replayed(tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    monkeypatch.setattr(
        replay,
        "replay_pair",
        lambda *a, **k: {
            "id": snap.name,
            "order": ["keep", "delegate"],
            "sides": {
                "keep": {"cost": 0.1, "wall_seconds": 1.0, "calls": 1, "warm": True, "delegated": False},
                "delegate": {"cost": 0.05, "wall_seconds": 1.0, "calls": 2, "warm": True, "delegated": True},
            },
            "inconclusive": False,
            "reason": "",
        },
    )
    assert fork_check.main(["replay", snap.name]) == 0
    assert fork_check.statuses()[snap.name]["status"] == "replayed"


def test_cmd_replay_marks_safe_inconclusive_with_reason(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    monkeypatch.setattr(
        replay,
        "replay_pair",
        lambda *a, **k: {
            "id": snap.name,
            "order": ["keep"],
            "sides": {"keep": {"attempt": 3, "clone": "x", "warm": False, "skipped": []}},
            "inconclusive": True,
            "reason": "crashed",
        },
    )
    assert fork_check.main(["replay", snap.name]) == 0
    status = fork_check.statuses()[snap.name]
    assert status["status"] == "inconclusive" and status["reason"] == "crashed"


def test_cmd_replay_marks_safe_replay_failed_and_clears_partial_results(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    leftover = tmp_path / "fc" / "results" / snap.name
    leftover.mkdir(parents=True)
    (leftover / "stray.txt").write_text("x\n")

    def boom(*a: object, **k: object) -> dict:
        raise RuntimeError("disk full")

    monkeypatch.setattr(replay, "replay_pair", boom)
    assert fork_check.main(["replay", snap.name]) == 1
    status = fork_check.statuses()[snap.name]
    assert status["status"] == "replay_failed" and "disk full" in status["reason"]
    assert not leftover.exists()


def test_a_job_whose_checkout_is_gone_is_inconclusive(
    tmp_path: Path, repo: Path, snap: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job captured in a worktree the user has since removed: restore says so,
    and replay records an inconclusive job with that reason, not a crash."""
    toplevel = json.loads((snap / "meta.json").read_text())["toplevel"]
    shutil.rmtree(repo)
    with pytest.raises(replay.Inconclusive) as excinfo:
        replay.restore(snap, tmp_path / "r")
    assert str(excinfo.value) == f"the checkout {toplevel} no longer exists"
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["mark", snap.name, "safe"]) == 0
    assert fork_check.main(["replay", snap.name]) == 0
    status = fork_check.statuses()[snap.name]
    assert status["status"] == "inconclusive" and "no longer exists" in status["reason"]
    assert not (tmp_path / "calls.jsonl").exists()  # nothing ran


def _fake_pair(replayed: list[str]) -> Callable[..., dict]:
    def fake_pair(snap: Path, *rest: object) -> dict:
        replayed.append(snap.name)
        sides = {"cost": 0.1, "wall_seconds": 1.0, "calls": 1, "warm": True, "delegated": True}
        return {
            "id": snap.name,
            "order": ["keep", "delegate"],
            "sides": {"keep": sides, "delegate": sides},
            "inconclusive": False,
            "reason": "",
        }

    return fake_pair


def _hold_lock(root: Path, sid: str) -> IO[str]:
    """What a live runner holds for the whole replay (F20)."""
    (root / "results").mkdir(parents=True, exist_ok=True)
    handle = (root / "results" / f"{sid}.lock").open("a")
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def _jobs(root: Path, *sids: str) -> None:
    for sid in sids:
        (root / "snapshots" / sid).mkdir(parents=True)
        (root / "snapshots" / sid / "meta.json").write_text("{}")


def test_a_job_left_replaying_by_an_interrupted_run_can_be_resumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """N5: `replay ID` accepts it, and `--next` picks it after the safe jobs; a
    job whose runner is still alive (it holds the job's lock) is left alone."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a", "20261001-100000-b", "20261001-110000-c")
    fork_check.set_status("20261001-090000-a", "replaying", "")  # interrupted: nobody holds its lock
    fork_check.set_status("20261001-100000-b", "safe")
    fork_check.set_status("20261001-110000-c", "replaying", "")
    running = _hold_lock(root, "20261001-110000-c")  # still running
    replayed: list[str] = []
    monkeypatch.setattr(replay, "replay_pair", _fake_pair(replayed))
    try:
        assert fork_check.main(["replay", "--next"]) == 0
        assert fork_check.main(["replay", "--next"]) == 0
        assert replayed == ["20261001-100000-b", "20261001-090000-a"]  # safe first, then the interrupted one
        assert fork_check.main(["replay", "--next"]) == 1  # only the running one is left
        assert fork_check.main(["replay", "20261001-110000-c"]) == 1
        assert "is being replayed by another runner right now" in capsys.readouterr().out
    finally:
        running.close()
    fork_check.set_status("20261001-090000-a", "replaying", "")
    assert fork_check.main(["replay", "20261001-090000-a"]) == 0
    assert fork_check.statuses()["20261001-090000-a"]["status"] == "replayed"


def test_next_never_takes_a_trial_job_and_names_the_running_ones_it_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """B2: a job with a trial replay anywhere in its history never enters the
    `--next` queue (a normal replay of it can never count), while `replay ID
    --trial` still resumes it; each job skipped because its runner is alive
    gets one line."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-080000-t1", "20261001-083000-t2", "20261001-090000-r", "20261001-100000-s")
    fork_check.set_status("20261001-080000-t1", "safe")
    fork_check.set_status("20261001-080000-t1", "replaying", "trial")  # an interrupted trial
    fork_check.set_status("20261001-083000-t2", "replaying", "trial")
    fork_check.set_status("20261001-083000-t2", "safe")  # marked safe again after its trial
    fork_check.set_status("20261001-090000-r", "replaying", "")
    fork_check.set_status("20261001-100000-s", "safe")
    replayed: list[str] = []
    monkeypatch.setattr(replay, "replay_pair", _fake_pair(replayed))
    running = _hold_lock(root, "20261001-090000-r")
    try:
        assert fork_check.main(["replay", "--next"]) == 0
        out = capsys.readouterr().out
        assert out.count("skipped, another runner is replaying it right now") == 1
        assert "20261001-090000-r: skipped" in out
        assert fork_check.main(["replay", "--next"]) == 1
    finally:
        running.close()
    assert replayed == ["20261001-100000-s"]
    assert fork_check.main(["replay", "20261001-080000-t1", "--trial"]) == 0
    assert replayed == ["20261001-100000-s", "20261001-080000-t1"]


def test_a_second_runner_of_the_same_job_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F20: the job's lock is held for the whole replay; a second runner stops
    before touching anything, and the job runs once the lock is free."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a")
    fork_check.set_status("20261001-090000-a", "safe")
    (root / "results" / "20261001-090000-a").mkdir(parents=True)
    (root / "results" / "20261001-090000-a" / "clone-in-use.txt").write_text("x\n")
    replayed: list[str] = []
    monkeypatch.setattr(replay, "replay_pair", _fake_pair(replayed))
    first = _hold_lock(root, "20261001-090000-a")
    try:
        assert fork_check.main(["replay", "20261001-090000-a"]) == 1
        assert "is being replayed by another runner right now" in capsys.readouterr().out
        assert (root / "results" / "20261001-090000-a" / "clone-in-use.txt").exists()
        assert replayed == [] and fork_check.statuses()["20261001-090000-a"]["status"] == "safe"
    finally:
        first.close()
    assert fork_check.main(["replay", "20261001-090000-a"]) == 0
    assert replayed == ["20261001-090000-a"]


def _leftover_claude(root: Path, sid: str) -> tuple[subprocess.Popen, Path]:
    """A process group like the one a killed runner leaves: its own session, and a
    command line with `--fork-session`. Returns it and its attempt folder."""
    leftover = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "--fork-session"], start_new_session=True
    )
    attempt = root / "results" / sid / "side-1" / "attempt-1"
    attempt.mkdir(parents=True)
    return leftover, attempt


@pytest.mark.parametrize("same_identity", [True, False])
def test_a_resume_stops_a_leftover_group_only_if_it_is_still_that_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], same_identity: bool
) -> None:
    """F20, F23d: a runner killed outright leaves its call's group id and the
    leader's identity in the attempt folder. The next run of the job kills the
    group only if `ps` still shows that start time and command line; a reused id
    (another start time) is left alone and its record dropped."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a")
    fork_check.set_status("20261001-090000-a", "replaying", "")
    leftover, attempt = _leftover_claude(root, "20261001-090000-a")
    record = attempt / "claude.pgid"
    replay._record_group(record, leftover.pid)
    if not same_identity:
        recorded = json.loads(record.read_text())
        record.write_text(json.dumps({**recorded, "lstart": "Thu Jan  1 00:00:00 1970"}))
    monkeypatch.setattr(replay, "replay_pair", _fake_pair([]))
    try:
        assert fork_check.main(["replay", "20261001-090000-a"]) == 0
        if same_identity:
            assert leftover.wait(timeout=5) == -signal.SIGKILL
        else:
            time.sleep(0.2)
            assert leftover.poll() is None  # somebody else's process now: never killed
    finally:
        if leftover.poll() is None:
            leftover.kill()
            leftover.wait()
    shown = f"stopped process group {leftover.pid}" in capsys.readouterr().out
    assert shown is same_identity
    assert not attempt.exists()


def test_a_resume_that_cannot_check_a_leftover_group_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F23d: without `ps`, a recorded group cannot be checked, so the job is not
    resumed and nothing is cleared."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a")
    fork_check.set_status("20261001-090000-a", "replaying", "")
    leftover, attempt = _leftover_claude(root, "20261001-090000-a")
    try:
        replay._record_group(attempt / "claude.pgid", leftover.pid)

        def no_ps(pid: int) -> tuple[str, str] | None:
            raise replay.IdentityUnavailable("ps could not run: [Errno 2] No such file or directory: 'ps'")

        monkeypatch.setattr(replay, "_identity", no_ps)
        monkeypatch.setattr(replay, "replay_pair", _fake_pair([]))
        assert fork_check.main(["replay", "20261001-090000-a"]) == 1
        assert "cannot check whether an earlier run's claude is still running" in capsys.readouterr().out
        assert (attempt / "claude.pgid").exists()
        assert leftover.poll() is None
        assert fork_check.statuses()["20261001-090000-a"]["status"] == "replaying"
    finally:
        leftover.kill()
        leftover.wait()


def _refused_resume(root: Path, sid: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    """Runs `replay sid` over a job left `replaying`, expects the refusal, and
    returns what it printed. Nothing ran, and the status is unchanged."""
    replayed: list[str] = []
    monkeypatch.setattr(replay, "replay_pair", _fake_pair(replayed))
    assert fork_check.main(["replay", sid]) == 1
    assert replayed == []
    assert fork_check.statuses()[sid]["status"] == "replaying"
    return capsys.readouterr().out


def test_a_resume_with_an_unreadable_group_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F30: a record cut short (by a hard kill before the atomic write existed,
    or by anything else) names no group anyone can check, so the resume stops
    and says where the record is and how to look at it."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a")
    fork_check.set_status("20261001-090000-a", "replaying", "")
    record = root / "results" / "20261001-090000-a" / "side-1" / "attempt-1" / "claude.pgid"
    record.parent.mkdir(parents=True)
    record.write_text('{"pgid": 12')
    out = _refused_resume(root, "20261001-090000-a", monkeypatch, capsys)
    assert f"{record} cannot be read" in out and f"cat {record}" in out
    assert record.exists()


def test_a_resume_is_refused_while_a_recorded_group_runs_without_its_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """F30: the `claude` leader exited, but a child it started (a dev server)
    still runs in its group. The leader's identity is gone, so the group cannot
    be checked: the resume stops, naming the record and how to look at the group."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    _jobs(root, "20261001-090000-a")
    fork_check.set_status("20261001-090000-a", "replaying", "")
    group = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!"], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    assert group.stdout is not None
    child = int(group.stdout.readline())
    group.wait()  # the leader is gone; its child keeps the group alive
    try:
        record = root / "results" / "20261001-090000-a" / "side-1" / "attempt-1" / "claude.pgid"
        record.parent.mkdir(parents=True)
        leader = {"pgid": group.pid, "lstart": "Tue Sep 29 10:00:00 2026", "command": "claude --fork-session -p"}
        record.write_text(json.dumps(leader) + "\n")
        out = _refused_resume(root, "20261001-090000-a", monkeypatch, capsys)
        assert str(record) in out and f"ps -o pid,pgid,lstart,command -g {group.pid}" in out
        assert record.exists()
        os.kill(child, 0)  # never killed
    finally:
        os.killpg(group.pid, signal.SIGKILL)
        group.stdout.close()


def test_a_group_record_is_written_whole_or_not_at_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F30: the record is written to a temp file in the same folder, then put in
    place with os.replace, so no half-written record is ever read."""
    record = tmp_path / "claude.pgid"

    def killed_here(*args: object) -> None:
        raise OSError("the runner died here")

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", killed_here)
        with pytest.raises(OSError, match="died here"):
            replay._record_group(record, os.getpid())
    assert not record.exists()
    replay._record_group(record, os.getpid())
    assert json.loads(record.read_text())["pgid"] == os.getpid()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["claude.pgid"]


def test_a_failed_group_record_still_kills_the_group(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F23c: the record is written inside the call's cleanup guard, so a write
    that raises still takes the group down."""
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_CHILD", str(tmp_path / "child.pid"))
    started: list[subprocess.Popen] = []

    class RememberedPopen(subprocess.Popen):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            started.append(self)

    monkeypatch.setattr(replay, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": RememberedPopen}))
    unwritable = tmp_path / "no-such-folder" / "claude.pgid"
    try:
        with pytest.raises(OSError):
            replay.run_claude(tmp_path, "sid", "hello", {}, fake_claude, "claude-opus-5-5", pgid_file=unwritable)
        (claude_call,) = started
        assert claude_call.returncode == -signal.SIGKILL
    finally:
        for proc in started:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def _interrupted_during_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: Callable[[], None]
) -> None:
    """Sets up the next claude call: its fake starts a `sleep 60` child (pid in
    tmp_path / "child.pid"), and the wait is interrupted with `interrupt()` once
    that child exists."""
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP_CHILD", str(pidfile))

    class InterruptedPopen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            deadline = time.monotonic() + 30
            while not pidfile.exists() and self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            interrupt()
            return super().communicate(input, timeout=30)

    monkeypatch.setattr(replay, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": InterruptedPopen}))


def _wait_until_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"process {pid} outlived the interrupted claude call")


def test_ctrl_c_during_a_claude_call_kills_its_process_group(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F20: the child runs in its own session, so the terminal's Ctrl-C never
    reaches it; the runner kills its whole group before the interrupt goes on."""

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    _interrupted_during_the_wait(tmp_path, monkeypatch, ctrl_c)
    pgid_file = tmp_path / "claude.pgid"
    with pytest.raises(KeyboardInterrupt):
        replay.run_claude(tmp_path, "sid", "hello", {}, fake_claude, "claude-opus-5-5", pgid_file=pgid_file)
    _wait_until_gone(int((tmp_path / "child.pid").read_text()))
    assert not pgid_file.exists()


@pytest.mark.parametrize("stop", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_a_stop_signal_during_a_claude_call_kills_its_process_group(
    tmp_path: Path, fake_claude: Path, monkeypatch: pytest.MonkeyPatch, stop: signal.Signals
) -> None:
    """F20, N2: SIGHUP (the terminal closing) is handled like SIGTERM, and both
    old handlers are put back afterwards. The old handlers here are no-ops, so
    the test holds even under `nohup`, and a missed signal cannot kill pytest."""

    def nothing(signum: int, frame: object) -> None:
        pass

    previous = {sig: signal.signal(sig, nothing) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:

        def send() -> None:
            os.kill(os.getpid(), stop)

        _interrupted_during_the_wait(tmp_path, monkeypatch, send)
        with pytest.raises(replay.Terminated):
            replay.run_claude(tmp_path, "sid", "hello", {}, fake_claude, "claude-opus-5-5")
        _wait_until_gone(int((tmp_path / "child.pid").read_text()))
        assert {sig: signal.getsignal(sig) for sig in previous} == dict.fromkeys(previous, nothing)
    finally:
        replay._put_back(previous)


def test_a_stop_signal_the_runner_ignores_stays_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under `nohup`, SIGHUP is SIG_IGN, and the runner leaves it so: a SIGHUP
    during a claude call changes nothing, while SIGTERM is still handled."""
    exe = tmp_path / "claude-hup"
    exe.write_text('#!/bin/sh\nkill -HUP "$PPID"\nsleep 0.3\necho "{}"\n')
    exe.chmod(0o755)
    monkeypatch.setenv("JEV_FORK_CHECK_CLAUDE", str(exe))
    during: dict[int, object] = {}

    class SeesTheHandlers(subprocess.Popen):
        def __init__(self, *args: object, **kwargs: object) -> None:
            during.update({sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM)})
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(replay, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": SeesTheHandlers}))
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        data, _, code, timed_out = replay.run_claude(tmp_path, "sid", "hello", {}, tmp_path / "home", "m")
        assert (data, code, timed_out) == ({}, 0, False)
        assert during == {signal.SIGHUP: signal.SIG_IGN, signal.SIGTERM: replay._raise_terminated}
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_an_unknown_or_escaping_id_is_refused_before_anything_is_touched(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`replay` deletes results/<id> before it starts: a typo, or an id holding
    `../`, must never reach that, nor any other command that takes an id."""
    root = tmp_path / "fc"
    monkeypatch.setattr(fork_check, "root", lambda: root)
    precious = root / "x"  # what results/../x names
    precious.mkdir()
    (precious / "keep.txt").write_text("mine\n")
    stray = root / "results" / "20990101-000000-nope"
    stray.mkdir(parents=True)
    for sid in ("../x", "20990101-000000-nope"):
        fork_check.set_status(sid, "safe")  # as `mark` used to accept any id
        for command in (["replay", sid], ["mark", sid, "safe"], ["judge", sid], ["publish", sid]):
            assert fork_check.main(command) == 1
    assert (precious / "keep.txt").read_text() == "mine\n"
    assert stray.exists()
    assert len((root / "status.jsonl").read_text().splitlines()) == 2  # `mark` wrote nothing
    assert "../x is not a snapshot id" in capsys.readouterr().out


def test_replay_requires_either_id_or_next() -> None:
    with pytest.raises(SystemExit):
        fork_check.main(["replay"])


def test_replay_rejects_both_id_and_next() -> None:
    with pytest.raises(SystemExit):
        fork_check.main(["replay", "some-id", "--next"])
