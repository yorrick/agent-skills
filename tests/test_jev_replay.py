"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

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
            self.warmup = cmd[-1] == replay.WARMUP
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
    assert [c["note"] is not None for c in calls if not prompt_of(c).startswith("Reply")] == [
        name == "delegate" for name in result["order"]
    ]
    # Ruling T11f New 2: the warm-up never gets the note, on either side.
    assert all(c["note"] is None for c in calls if prompt_of(c).startswith("Reply"))
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
    assert calls and all(prompt_of(c).startswith("Reply") for c in calls)


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
    assert [(prompt_of(c) == replay.WARMUP, c.get("hung", False)) for c in calls] == [(True, False), (False, True)]


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
    assert [prompt_of(c) == replay.WARMUP for c in calls_log(tmp_path)] == [True, False]
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
    job_calls = [c for c in calls if not prompt_of(c).startswith("Reply")]
    assert job_calls
    for c in job_calls:
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


def test_replay_requires_either_id_or_next() -> None:
    with pytest.raises(SystemExit):
        fork_check.main(["replay"])


def test_replay_rejects_both_id_and_next() -> None:
    with pytest.raises(SystemExit):
        fork_check.main(["replay", "some-id", "--next"])
