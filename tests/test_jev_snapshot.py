"""Fork-check snapshots: the conversation and working copy just before a job."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import snapshot  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402

EVENT = {
    "context": 803_010,
    "model": "claude-opus-5-5",
    "helper": "jev-router:large",
    "expected_saving": 1.2,
    "loss_probability": 0.0,
    "median_calls": 30,
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def payload(repo: Path, transcript: Path) -> dict:
    return {"session_id": "abcdef1234", "transcript_path": str(transcript), "cwd": str(repo)}


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_snapshot_holds_the_conversation_and_the_working_copy(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text="Ready.")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "NOTE", EVENT)
    d = tmp_path / "fc" / "snapshots" / sid
    meta = json.loads((d / "meta.json").read_text())
    assert meta["head"] == git(repo, "rev-parse", "HEAD").strip()
    assert meta["branch"] == "main" and meta["toplevel"] == str(repo.resolve())
    assert meta["helper"] == "jev-router:large" and meta["context"] == 803_010
    # Ruling F3': the ignored entries a restore may copy, paths only.
    assert meta["ignored_entries"] == [".env", "node_modules"]
    assert (d / "message.txt").read_text() == "build it"
    assert (d / "note.txt").read_text() == "NOTE"
    assert "print('v2')" in (d / "changes.diff").read_text()
    with tarfile.open(d / "untracked.tar") as tar:
        assert tar.getnames() == ["notes.md"]
    assert (d / "transcript.jsonl").read_text() == transcript.read_text()


def meta_of(tmp_path: Path, sid: str) -> dict:
    return json.loads((tmp_path / "fc" / "snapshots" / sid / "meta.json").read_text())


def test_snapshot_cuts_the_prompt_if_already_written(tmp_path: Path, repo: Path) -> None:
    before = [typed("start"), assistant("r1", text="Ready.")]
    transcript = write(tmp_path / "t.jsonl", [*before, typed("build it")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    kept = (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text()
    assert kept == "".join(json.dumps(e) + "\n" for e in before)
    assert meta_of(tmp_path, sid)["prompt_cut"] is True


def test_snapshot_keeps_everything_if_prompt_not_written_yet(tmp_path: Path, repo: Path) -> None:
    entries = [typed("build it"), assistant("r1", text="Done."), typed("something else")]
    transcript = write(tmp_path / "t.jsonl", entries)
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    assert (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text() == transcript.read_text()
    assert meta_of(tmp_path, sid)["prompt_cut"] is False


def test_snapshot_keeps_an_earlier_identical_prompt(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [typed("continue"), assistant("r1", text="Done.")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "continue", "N", EVENT)
    assert (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text() == transcript.read_text()
    assert meta_of(tmp_path, sid)["prompt_cut"] is False


def test_snapshot_keeps_the_earlier_identical_prompt_even_with_a_later_one(tmp_path: Path, repo: Path) -> None:
    """ "continue" typed, answered, then typed again: the message is already written
    (the last typed entry), but an earlier identical "continue" must not fool the
    cut into keeping only up to itself; the first two lines (the earlier exchange)
    are what stays."""
    before = [typed("continue"), assistant("r1")]
    transcript = write(tmp_path / "t.jsonl", [*before, typed("continue")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "continue", "N", EVENT)
    kept = (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text()
    assert kept == "".join(json.dumps(e) + "\n" for e in before)


def test_fingerprint_ignores_virtualenvs_but_sees_node_packages(repo: Path) -> None:
    """A replay never copies a virtualenv (`uv run` rebuilds it), so a change to
    the real one must not make a job inconclusive; node_modules is copied."""
    packages = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    packages.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
    before = snapshot.ignored_fingerprint(repo)
    (packages / "requests").mkdir()
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /somewhere/else\n")
    assert snapshot.ignored_fingerprint(repo) == before
    (repo / "node_modules" / ".package-lock.json").write_text('{"packages": {}}')
    assert snapshot.ignored_fingerprint(repo) != before


def test_fingerprint_over_a_given_list_skips_entries_outside_it(repo: Path) -> None:
    before = snapshot.ignored_fingerprint(repo, entries=["node_modules"])
    (repo / ".env").write_text("TOKEN=y\n")
    assert snapshot.ignored_fingerprint(repo, entries=["node_modules"]) == before
    assert snapshot.ignored_fingerprint(repo, entries=[".env", "node_modules"]) != before


def test_fingerprint_follows_dependencies_and_env_files_only(repo: Path) -> None:
    before = snapshot.ignored_fingerprint(repo)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"0")
    assert snapshot.ignored_fingerprint(repo) == before
    (repo / ".env").write_text("TOKEN=y\n")
    assert snapshot.ignored_fingerprint(repo) != before


def test_outside_a_git_repository_there_is_no_snapshot(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.take_snapshot(tmp_path / "fc", payload(plain, transcript), "x", "N", EVENT)


def test_huge_untracked_files_are_refused(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "MAX_UNTRACKED_BYTES", 3)
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    with pytest.raises(snapshot.SnapshotError, match="untracked"):
        snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)


# --- untracked nested repositories and worktrees (Ruling T7a) ------------------------


def test_untracked_nested_git_repositories_are_skipped_not_tarred(tmp_path: Path, repo: Path) -> None:
    """`git ls-files --others` reports a directory holding its own .git (a nested
    repo or a worktree) as a single entry, never descended into. Walking it with
    tar.add would recurse through its whole tree in one uninterruptible call, so it
    must be skipped, not tarred, and named in meta.json instead."""
    nested = repo / "vendor" / "sub"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    (nested / "file.txt").write_text("x\n")
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)
    d = tmp_path / "fc" / "snapshots" / sid
    with tarfile.open(d / "untracked.tar") as tar:
        names = tar.getnames()
    assert not any(n.startswith("vendor/sub") for n in names)
    assert meta_of(tmp_path, sid)["skipped"] == ["vendor/sub"]


def test_a_snapshot_with_nothing_to_skip_lists_no_skipped_paths(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)
    assert meta_of(tmp_path, sid)["skipped"] == []


# --- deadlines and the atomic .tmp folder (Ruling P4) --------------------------------


def test_a_deadline_already_past_raises_and_leaves_nothing_behind(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    root = tmp_path / "fc"
    with pytest.raises(snapshot.SnapshotError, match="out of time"):
        snapshot.take_snapshot(root, payload(repo, transcript), "x", "N", EVENT, deadline=time.monotonic() - 1)
    assert not (root / "snapshots").exists()


def test_a_successful_snapshot_leaves_no_tmp_folder(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)
    names = [p.name for p in (tmp_path / "fc" / "snapshots").iterdir()]
    assert names == [sid]


def test_a_failure_after_the_tmp_folder_exists_still_leaves_nothing_behind(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ignored_fingerprint` runs last, well after `.tmp` has been created and every
    other file written, so failing there exercises cleanup of a `.tmp` folder that
    is not empty, unlike the deadline-already-past case above."""

    def boom(top: Path, deadline: float | None = None, **kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(snapshot, "ignored_fingerprint", boom)
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    with pytest.raises(RuntimeError, match="boom"):
        snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)
    assert list((tmp_path / "fc" / "snapshots").iterdir()) == []
