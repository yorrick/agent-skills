"""A blind judge, and the fork check's pass or fail."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import judge  # noqa: E402
import replay  # noqa: E402
import report  # noqa: E402
import snapshot  # noqa: E402

ANSWER = (
    'Both fine.\n{"A": {"tests": "pass", "outcome_met": true}, "B": {"tests": "fail", "outcome_met": false}, '
    '"prefer": "A", "why": "B broke a test."}'
)


def test_parse_takes_the_last_json_line() -> None:
    assert judge.parse(ANSWER)["prefer"] == "A"


def test_parse_refuses_a_malformed_verdict() -> None:
    bad = '{"A": {"tests": "pass", "outcome_met": "false"}, "B": {"tests": "pass", "outcome_met": true}, "prefer": "A"}'
    with pytest.raises(ValueError):
        judge.parse(bad)
    with pytest.raises(ValueError):
        judge.parse(ANSWER.replace('"prefer": "A"', '"prefer": "keep"'))
    # Ruling T13d: the last JSON line is final; a well-formed draft earlier in the
    # reply must never stand in for a malformed one that comes after it.
    draft_then_malformed_final = (
        ANSWER + '\n{"A": {"tests": "pass", "outcome_met": true}, '
        '"B": {"tests": "passed", "outcome_met": true}, "prefer": "B"}'
    )
    with pytest.raises(ValueError):
        judge.parse(draft_then_malformed_final)


def test_labels_are_mapped_back_to_keep_and_delegate(tmp_path: Path, snap: Path) -> None:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    seen: dict = {}

    def codex(prompt: str, work: Path) -> str:
        seen["prompt"], seen["dirs"] = prompt, sorted(p.name for p in work.iterdir())
        return ANSWER

    # cost is present on both sides so this test exercises label mapping only,
    # not the T13a refusal checks (covered separately below).
    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    rng = random.Random(3)
    labels = ["keep", "delegate"]
    random.Random(3).shuffle(labels)  # the same draw judge() makes: A gets labels[0]
    verdict = judge.judge(snap, result, tmp_path / "j", codex=codex, rng=rng)
    assert verdict[labels[0]] == {"tests": "pass", "outcome_met": True}
    assert verdict["prefer"] == labels[0]
    assert "LEAF" in seen["prompt"] and "refs/jev/start" in seen["prompt"]
    assert seen["dirs"] == ["A", "B"]
    for letter in ("A", "B"):
        remotes = subprocess.run(
            ["git", "-C", str(tmp_path / "j" / letter), "remote"], capture_output=True, text=True
        ).stdout
        assert remotes == ""
    for letter in ("A", "B"):
        for f in (tmp_path / "j" / letter / ".git").rglob("*"):
            if f.is_file():
                data = f.read_bytes()
                assert str(tmp_path / "k").encode() not in data and str(tmp_path / "d").encode() not in data


def test_blind_copies_leave_out_the_restored_secrets_but_keep_dependencies(tmp_path: Path, snap: Path) -> None:
    """F18: the judge is a model, so `.env` never reaches A or B; node_modules
    stays so the project's tests can run. The clones keep their own `.env`."""
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    seen: dict = {}

    def codex(prompt: str, work: Path) -> str:
        seen["env"] = [(work / letter / ".env").exists() for letter in ("A", "B")]
        return ANSWER

    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(3))
    assert seen["env"] == [False, False]
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert not os.path.lexists(copy / ".env")
        assert (copy / "node_modules" / ".package-lock.json").read_text() == "{}"
        assert (copy / "app.py").read_text() == "print('v2')\n"
        assert (copy / ".git").is_dir()
    assert (keep / ".env").read_text() == "TOKEN=x\n" and (delegate / ".env").read_text() == "TOKEN=x\n"


def _judge_both(tmp_path: Path, snap: Path, keep: Path, delegate: Path) -> None:
    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    judge.judge(snap, result, tmp_path / "j", codex=lambda prompt, work: ANSWER, rng=random.Random(3))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_judge_copies_leave_out_what_the_clone_ignores_and_every_copy_of_a_secret(tmp_path: Path, snap: Path) -> None:
    """F23a: `.env` moved to `.env.local` (ignored), copied into `.venv/saved.env`
    (ignored, not a dependency folder), and copied into `node_modules` (a
    dependency folder, kept, so found by its content) never reach A or B."""
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        shutil.copy(clone / ".env", clone / "node_modules" / "leak.env")
        (clone / ".venv").mkdir()
        shutil.copy(clone / ".env", clone / ".venv" / "saved.env")
        (clone / ".env").rename(clone / ".env.local")
        (clone / ".gitignore").write_text((clone / ".gitignore").read_text() + ".env.local\n")
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        for gone in (".env", ".env.local", ".venv", "node_modules/leak.env"):
            assert not os.path.lexists(copy / gone), gone
        assert (copy / "node_modules" / ".package-lock.json").read_text() == "{}"
        assert (copy / "notes.md").read_text() == "draft\n"


def _snapshot_of(tmp_path: Path, repo: Path) -> Path:
    from test_jev_snapshot import EVENT, payload, write
    from test_jev_usage import assistant, typed

    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    return tmp_path / "fc" / "snapshots" / sid


def test_a_four_byte_env_moved_to_another_name_is_refused_with_no_codex_call(tmp_path: Path, repo: Path) -> None:
    """F32: restore records every restored secret up to 1 MB, however small, so
    a 4-byte `.env` that is gone by check time is still known."""
    (repo / ".env").write_text("A=1\n")
    snap = _snapshot_of(tmp_path, repo)
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    (delegate / ".env").rename(delegate / "settings.txt")
    _refused_with_no_codex_call(
        tmp_path, snap, keep, delegate, r"settings\.txt in the delegate clone holds the content of .* \.env"
    )


def test_a_tracked_file_equal_to_a_restored_secret_stays_in_the_judge_copy(tmp_path: Path, repo: Path) -> None:
    """F32: content the user's committed history already holds is never a
    secret, for the copy as for the refusal: `.env` was made from the tracked
    `.env.example`, which the judge still gets."""
    (repo / ".env.example").write_text("TOKEN=x\n")  # the same bytes as the fixture's .env
    assert _git(repo, "add", ".env.example").returncode == 0
    assert _git(repo, "commit", "-qm", "an example env").returncode == 0
    snap = _snapshot_of(tmp_path, repo)
    _judge_both(tmp_path, snap, replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d"))
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / ".env.example").read_text() == "TOKEN=x\n"
        assert not os.path.lexists(copy / ".env")


def test_empty_files_stay_even_when_a_restored_ignored_file_is_empty(tmp_path: Path, repo: Path) -> None:
    """F32: the empty file is never a secret. An empty restored `.next/turbopack`
    neither refuses a new empty `__init__.py` nor keeps an empty file in
    `node_modules` from the judge."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".next/\n")
    assert _git(repo, "add", ".gitignore").returncode == 0
    assert _git(repo, "commit", "-qm", "ignore .next").returncode == 0
    (repo / ".next").mkdir()
    (repo / ".next" / "turbopack").write_bytes(b"")
    (repo / "node_modules" / "pkg").mkdir()
    (repo / "node_modules" / "pkg" / "index.d.ts").write_bytes(b"")
    snap = _snapshot_of(tmp_path, repo)
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        assert (clone / ".next" / "turbopack").exists()
        (clone / "src").mkdir()
        (clone / "src" / "__init__.py").write_bytes(b"")  # a new, untracked empty file
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / "node_modules" / "pkg" / "index.d.ts").read_bytes() == b""
        assert (copy / "src" / "__init__.py").read_bytes() == b""
        assert not os.path.lexists(copy / ".next")


def test_stashed_staged_or_side_branch_secrets_never_reach_the_judge(tmp_path: Path, repo: Path, snap: Path) -> None:
    """F24: the copies keep only HEAD's history and refs/jev/start, and `git gc
    --prune=now` drops the rest: a stash holding `.env`, a blob staged then
    reset, and a side branch the replay committed `.env` to."""
    env_blob = _git(repo, "hash-object", ".env").stdout.strip()
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    who = ["-c", "user.name=x", "-c", "user.email=x@x"]
    assert _git(keep, "add", "-f", ".env").returncode == 0
    assert _git(keep, *who, "stash").returncode == 0
    assert _git(delegate, "add", "-f", ".env").returncode == 0
    assert _git(delegate, "reset", "-q").returncode == 0
    assert _git(delegate, "checkout", "-q", "-b", "side").returncode == 0
    assert _git(delegate, "add", "-f", ".env").returncode == 0
    assert _git(delegate, *who, "commit", "-qm", "the replay kept .env on a side branch").returncode == 0
    assert _git(delegate, "checkout", "-q", "main").returncode == 0
    for clone in (keep, delegate):
        assert _git(clone, "cat-file", "-e", env_blob).returncode == 0  # the secret is in both clones' objects
    _judge_both(tmp_path, snap, keep, delegate)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert _git(copy, "cat-file", "-e", env_blob).returncode != 0
        assert _git(copy, "rev-parse", "-q", "--verify", "refs/stash").returncode != 0
        assert _git(copy, "for-each-ref", "--format=%(refname)").stdout.split() == ["refs/heads/main", "refs/jev/start"]
        assert _git(copy, "rev-parse", "-q", "--verify", "refs/jev/start^").returncode == 0


# Ruling T13a: an inconclusive result, or a side missing a priced cost, is
# refused before anything is copied or codex is ever called.
def test_inconclusive_result_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {"id": "x", "sides": {}, "inconclusive": True, "reason": "no model recorded"}
    with pytest.raises(RuntimeError, match="inconclusive"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def test_a_side_missing_cost_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate)}},
    }
    with pytest.raises(RuntimeError, match="cost"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


# Ruling T13a: a clone whose .gitignore no longer ignores a restored path is
# refused before codex is ever called, whether or not it was committed yet.
def test_a_clone_that_stopped_ignoring_env_is_refused_with_no_codex_call(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # mask the user's own global excludes (it may list .env)
    (keep / ".gitignore").write_text("node_modules/\n.venv/\n__pycache__/\n")  # the replay dropped the .env line
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"\.env"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


# Ruling F5: the judge never sees a restored ignored file's content under another name.
def test_a_copy_of_a_restored_env_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    shutil.copy(delegate / ".env", delegate / "config.txt")  # untracked, never committed: the judge never commits
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"config\.txt in the delegate clone holds the content of .* \.env"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []
    assert not (tmp_path / "j").exists()


def test_a_restored_env_moved_to_another_name_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    """Ruling F10: `mv .env config.txt` is caught from the content restore recorded."""
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    (delegate / ".env").rename(delegate / "config.txt")
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"config\.txt in the delegate clone holds the content of .* \.env"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []
    assert not (tmp_path / "j").exists()


def test_a_restored_env_edited_then_copied_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    """N1: the restored files are hashed again at check time, alongside the ids restore recorded."""
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    with (delegate / ".env").open("a") as env:
        env.write("EXTRA=1\n")
    shutil.copy(delegate / ".env", delegate / "config.txt")
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"config\.txt in the delegate clone holds the content of .* \.env"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def test_a_copy_in_history_matching_an_untracked_start_file_is_refused_with_no_codex_call(
    tmp_path: Path, repo: Path
) -> None:
    """F17, through the judge: it reads the same history as publish."""
    from test_jev_publish import _snapshot_with_an_untracked_copy_of_env, commit_then_remove_a_copy_of_env

    snap = _snapshot_with_an_untracked_copy_of_env(tmp_path, repo)
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    commit_then_remove_a_copy_of_env(delegate)
    (keep / "copy.txt").unlink()  # else keep is refused first, for its untracked copy (F13)
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"config\.txt in the delegate clone holds the content of .* \.env"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def _refused_with_no_codex_call(tmp_path: Path, snap: Path, keep: Path, delegate: Path, match: str) -> None:
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=match):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []
    assert not (tmp_path / "j").exists()


def test_a_secret_moved_to_a_name_holding_a_newline_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    """F27: blob ids are computed in Python, so a newline in a name can no
    longer slip past `git hash-object --stdin-paths`, which reads one path per line."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    (delegate / ".env").rename(delegate / "con\nfig.txt")
    _refused_with_no_codex_call(
        tmp_path, snap, keep, delegate, r"(?s)con\nfig\.txt in the delegate clone holds the content of .* \.env"
    )


@pytest.mark.parametrize("then", ["deleted", "edited"])
def test_a_staged_copy_of_a_secret_is_refused_with_no_codex_call(tmp_path: Path, snap: Path, then: str) -> None:
    """F26: `cp .env config.txt && git add config.txt`, then the working file is
    deleted or edited. The staged blob is still in `.git/index`, where the
    judge could read it with `git show :config.txt`."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    shutil.copy(delegate / ".env", delegate / "config.txt")
    assert _git(delegate, "add", "config.txt").returncode == 0
    if then == "deleted":
        (delegate / "config.txt").unlink()
    else:
        (delegate / "config.txt").write_text("nothing secret here\n")
    _refused_with_no_codex_call(
        tmp_path, snap, keep, delegate, r"config\.txt in the delegate clone holds the content of .* \.env"
    )


def test_a_blob_only_ever_staged_never_reaches_the_judge(tmp_path: Path, snap: Path) -> None:
    """F26, F33: the copies get a fresh index built from HEAD, and a `.git`
    fetched from the clone rather than copied, so no blob that was staged and
    then deleted or edited reaches them."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    staged = []
    for clone, name in ((keep, "gone.txt"), (delegate, "draft.txt")):
        (clone / name).write_text(f"staged in {name}, then changed\n")
        assert _git(clone, "add", name).returncode == 0
        staged.append(_git(clone, "rev-parse", f":{name}").stdout.strip())
    (keep / "gone.txt").unlink()
    (delegate / "draft.txt").write_text("final\n")
    (delegate / ".git" / "index.lock").write_text("")  # a killed replay's stale lock never stops the rebuild
    _judge_both(tmp_path, snap, keep, delegate)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        for blob in staged:
            assert _git(copy, "cat-file", "-e", blob).returncode != 0
        assert _git(copy, "diff", "--cached", "--quiet", "HEAD").returncode == 0  # the index is HEAD's
    assert {(tmp_path / "j" / letter / "draft.txt").exists() for letter in ("A", "B")} == {True, False}


def test_a_secret_written_inside_the_clones_git_never_reaches_the_judge(tmp_path: Path, snap: Path) -> None:
    """F33: the copies' `.git` is fetched from the clone, never copied, so a
    file the replay wrote in there (`cp .env .git/notes.txt`) stays behind:
    nothing under A or B holds the secret's bytes, or its blob."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    secret = (keep / ".env").read_bytes()
    env_blob = _git(keep, "hash-object", ".env").stdout.strip()
    for clone in (keep, delegate):
        (clone / ".git" / "notes.txt").write_bytes(secret)
    _judge_both(tmp_path, snap, keep, delegate)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert not os.path.lexists(copy / ".git" / "notes.txt")
        held = [f for f in copy.rglob("*") if f.is_file() and not f.is_symlink() and secret in f.read_bytes()]
        assert held == []
        assert _git(copy, "cat-file", "-e", env_blob).returncode != 0


def test_a_nested_git_the_replay_made_never_reaches_the_judge(tmp_path: Path, snap: Path) -> None:
    """F33: a nested repository's `.git` is left out too, its working files
    stay. The replay committed a copy of `.env` in `vendor/lib` and removed the
    file, so only that repository's objects hold it; and `linked/.git` is a
    gitfile pointing straight at the clone's own `.git`."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    who = ["-c", "user.name=x", "-c", "user.email=x@x"]
    for clone in (keep, delegate):
        nested = clone / "vendor" / "lib"
        nested.mkdir(parents=True)
        assert _git(nested, "init", "-q").returncode == 0
        (nested / "README.md").write_text("a vendored library\n")
        shutil.copy(clone / ".env", nested / "config.txt")
        assert _git(nested, "add", ".").returncode == 0
        assert _git(nested, *who, "commit", "-qm", "vendor it").returncode == 0
        (nested / "config.txt").unlink()
        (clone / "linked").mkdir()
        (clone / "linked" / ".git").write_text(f"gitdir: {clone / '.git'}\n")
        (clone / "linked" / "notes.md").write_text("linked notes\n")
    _judge_both(tmp_path, snap, keep, delegate)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert not os.path.lexists(copy / "vendor" / "lib" / ".git")
        assert not os.path.lexists(copy / "linked" / ".git")
        assert (copy / "vendor" / "lib" / "README.md").read_text() == "a vendored library\n"
        assert (copy / "linked" / "notes.md").read_text() == "linked notes\n"


def test_a_detached_head_stays_detached_in_the_judge_copy(tmp_path: Path, snap: Path) -> None:
    """F33: a replay that left HEAD detached gets a detached copy at the same
    commit, even where a branch points at that commit (clone would otherwise
    put the copy on that branch), and no branch ref at all."""
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    assert _git(keep, "checkout", "-q", "--detach").returncode == 0  # main points here too
    assert _git(delegate, "checkout", "-q", "--detach").returncode == 0
    who = ["-c", "user.name=x", "-c", "user.email=x@x"]
    assert _git(delegate, *who, "commit", "-q", "--allow-empty", "-m", "detached only").returncode == 0
    _judge_both(tmp_path, snap, keep, delegate)
    heads = sorted(_git(tmp_path / "j" / letter, "rev-parse", "HEAD").stdout for letter in ("A", "B"))
    assert heads == sorted(_git(clone, "rev-parse", "HEAD").stdout for clone in (keep, delegate))
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert _git(copy, "symbolic-ref", "-q", "HEAD").returncode != 0
        assert _git(copy, "for-each-ref", "--format=%(refname)").stdout.split() == ["refs/jev/start"]


def test_the_judges_diff_command_shows_exactly_the_replays_changes(tmp_path: Path, repo: Path) -> None:
    """F33: with a fresh `.git` and an index built from HEAD, status shows the
    replay's modifications, additions and deletions, and the judge's documented
    command (`git -C A add -A && git -C A diff --cached refs/jev/start`) shows
    exactly its changes: nothing from `node_modules`, even one ignored only
    through `.git/info/exclude`."""
    (repo / "old.txt").write_text("to be deleted\n")
    (repo / ".gitignore").write_text(".venv/\n.env\n__pycache__/\n")  # node_modules/ moves to info/exclude
    assert _git(repo, "add", "old.txt", ".gitignore").returncode == 0
    assert _git(repo, "commit", "-qm", "an old file; node_modules ignored locally").returncode == 0
    with (repo / ".git" / "info" / "exclude").open("a") as exclude:
        exclude.write("node_modules/\n")
    snap = _snapshot_of(tmp_path, repo)
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    who = ["-c", "user.name=x", "-c", "user.email=x@x"]
    for clone in clones:
        assert (clone / "node_modules" / ".package-lock.json").exists()
        (clone / "committed.txt").write_text("committed by the replay\n")
        assert _git(clone, "add", "committed.txt").returncode == 0
        assert _git(clone, *who, "commit", "-qm", "the replay's commit").returncode == 0
        (clone / "app.py").write_text("print('v3')\n")
        (clone / "old.txt").unlink()
        (clone / "new.txt").write_text("a new file\n")
    _judge_both(tmp_path, snap, *clones)
    heads = {letter: _git(tmp_path / "j" / letter, "rev-parse", "HEAD").stdout for letter in ("A", "B")}
    assert sorted(heads.values()) == sorted(_git(clone, "rev-parse", "HEAD").stdout for clone in clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / "node_modules" / ".package-lock.json").exists()
        assert _git(copy, "symbolic-ref", "HEAD").stdout == "refs/heads/main\n"
        # Against HEAD, so the start's own untracked notes.md shows too.
        status = _git(copy, "status", "--porcelain").stdout.splitlines()
        assert sorted(status) == [" D old.txt", " M app.py", "?? new.txt", "?? notes.md"]
        documented = subprocess.run(
            f"git -C {letter} add -A && git -C {letter} diff --cached --name-status refs/jev/start",
            shell=True,
            cwd=copy.parent,
            capture_output=True,
            text=True,
        )
        assert documented.returncode == 0, documented.stderr
        assert documented.stdout.splitlines() == ["M\tapp.py", "A\tcommitted.txt", "A\tnew.txt", "D\told.txt"]


# F14: a replay-made link into the fork-check folder would unblind the judge.
def _make_venv(clone: Path, interpreter: Path) -> Path:
    """What `uv sync` leaves in a clone: an ignored `.venv` holding `pyvenv.cfg`,
    an absolute link to the interpreter, and installed packages."""
    venv = clone / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {interpreter.parent}\n")
    (venv / "bin" / "python").symlink_to(interpreter)
    (venv / "lib" / "site-packages").mkdir(parents=True)
    (venv / "lib" / "site-packages" / "pkg.py").write_text("x = 1\n")
    return venv


def _link_case(tmp_path: Path, snap: Path, target: Path) -> tuple[dict, list[str], Callable[[str, Path], str]]:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    # Reason: inside a virtualenv, where the judge's copy keeps links that
    # point out of it (Ruling F29), so only F14's check stands in the way.
    _make_venv(delegate, target)
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    return result, calls, codex


def test_a_link_into_the_fork_check_folder_is_refused_with_no_codex_call(tmp_path: Path, snap: Path) -> None:
    fork_root = snap.parent.parent
    mapping = fork_root / "results" / snap.name / "result.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text('{"order": ["keep", "delegate"]}\n')
    result, calls, codex = _link_case(tmp_path, snap, mapping)
    with pytest.raises(RuntimeError, match=r"\.venv/bin/python, a link into the fork-check folder"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def test_a_virtualenv_the_replay_made_stays_in_the_judge_copy(tmp_path: Path, snap: Path) -> None:
    """A folder holding `pyvenv.cfg` at its root is kept like a dependency
    folder, links inside it included, so the judge can run Python tests: the
    `.venv` the replay built (ignored), and one inside an ignored `.tox`, whose
    other contents are still left out. A copy of a secret inside a kept
    virtualenv is still left out, by its content."""
    interpreter = tmp_path / "uv-python" / "bin" / "python3.12"
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        shutil.copy(clone / ".env", _make_venv(clone, interpreter) / "saved.env")
        (clone / ".gitignore").write_text((clone / ".gitignore").read_text() + ".tox/\n")
        (clone / ".tox" / "py312").mkdir(parents=True)
        (clone / ".tox" / "py312" / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (clone / ".tox" / "log.txt").write_text("a tox run log\n")
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / ".venv" / "pyvenv.cfg").read_text() == f"home = {interpreter.parent}\n"
        assert os.readlink(copy / ".venv" / "bin" / "python") == str(interpreter)
        assert (copy / ".venv" / "lib" / "site-packages" / "pkg.py").read_text() == "x = 1\n"
        assert not os.path.lexists(copy / ".venv" / "saved.env")
        assert (copy / ".tox" / "py312" / "pyvenv.cfg").exists()
        assert not os.path.lexists(copy / ".tox" / "log.txt")


def test_the_replays_virtualenv_stays_whole_when_the_user_has_one_too(tmp_path: Path, repo: Path) -> None:
    """The user's own `.venv` is a captured ignored entry that restore never
    copies (Ruling T9d), so the replay's `uv sync` builds its own where a
    restored entry would sit. Nothing in it was restored, so none of its files
    counts as a secret, and the judge's copy keeps it whole."""
    interpreter = tmp_path / "uv-python" / "bin" / "python3.12"
    _make_venv(repo, interpreter)
    snap = _snapshot_of(tmp_path, repo)
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        assert not os.path.lexists(clone / ".venv")
        _make_venv(clone, interpreter)
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / ".venv" / "pyvenv.cfg").read_text() == f"home = {interpreter.parent}\n"
        assert (copy / ".venv" / "lib" / "site-packages" / "pkg.py").read_text() == "x = 1\n"


def _outside_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "home" / ".ssh" / "id_rsa"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE KEY\n")
    return secret


@pytest.mark.parametrize("how", ["tracked", "untracked"])
def test_a_link_out_in_the_result_refuses_the_judge(tmp_path: Path, snap: Path, how: str) -> None:
    """F37: a link out of the repository that is part of the replay's result
    (`key -> ~/.ssh/id_rsa`, committed, or untracked and not ignored) is never
    removed silently: the judge would measure a result that is not the
    replay's. Judging is refused before codex runs, and nothing is touched."""
    secret = _outside_secret(tmp_path)
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    (delegate / "docs").mkdir()
    (delegate / "docs" / "key").symlink_to(secret if how == "tracked" else "../../../home/.ssh/id_rsa")
    if how == "tracked":
        assert _git(delegate, "add", "docs/key").returncode == 0
        assert _git(delegate, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "a key").returncode == 0
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {
        "id": "x",
        "sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}},
    }
    with pytest.raises(RuntimeError, match=r"the result holds a link outside the repository: docs/key"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []
    assert secret.read_text() == "PRIVATE KEY\n" and os.path.lexists(delegate / "docs" / "key")


def test_links_out_outside_the_result_never_stop_the_judge(tmp_path: Path, snap: Path) -> None:
    """F29, F37: an ignored link out (`__pycache__/key`) never reaches A or B
    and does not stop the judge. A link that stays inside the copy, a venv's
    interpreter link, a link in `node_modules`, and a result link that only
    leads out through the venv's own link (`python -> .venv/bin/python`) are
    kept."""
    secret = _outside_secret(tmp_path)
    interpreter = tmp_path / "uv-python" / "bin" / "python3.12"
    tool = tmp_path / "global" / "bin" / "tool"
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        (clone / "__pycache__").mkdir()
        (clone / "__pycache__" / "key").symlink_to(secret)
        (clone / "docs").mkdir()
        (clone / "docs" / "app.py").symlink_to("../app.py")  # stays inside
        _make_venv(clone, interpreter)
        (clone / "python").symlink_to(".venv/bin/python")
        (clone / "node_modules" / ".bin").mkdir()
        (clone / "node_modules" / ".bin" / "tool").symlink_to(tool)
        (clone / "tool").symlink_to("node_modules/.bin/tool")
        (clone / "docs" / "loop").symlink_to("loop")  # leads nowhere
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert not os.path.lexists(copy / "__pycache__")
        assert os.readlink(copy / "docs" / "app.py") == "../app.py"
        assert os.readlink(copy / "python") == ".venv/bin/python"
        assert os.readlink(copy / "tool") == "node_modules/.bin/tool"
        assert os.readlink(copy / "docs" / "loop") == "loop"
        assert os.readlink(copy / ".venv" / "bin" / "python") == str(interpreter)
        assert os.readlink(copy / "node_modules" / ".bin" / "tool") == str(tool)
    assert secret.read_text() == "PRIVATE KEY\n"
    assert all(os.readlink(clone / "__pycache__" / "key") == str(secret) for clone in clones)


@pytest.mark.parametrize(
    "esc",
    ["up/../../home/.ssh/id_rsa", "../../../.venv/../a/b/c/up/../../home/.ssh/id_rsa"],
    ids=["chain", "chain-through-a-venv-by-name"],
)
def test_result_links_that_look_inside_but_chain_out_are_refused(tmp_path: Path, snap: Path, esc: str) -> None:
    """N-a: `a/b/c/up -> ../../..` and `a/b/c/esc -> up/../../home/.ssh/id_rsa`
    both stay inside by their text, but `..` after a link climbs from the
    link's target, so `esc` reads a private key outside the copy. Naming a
    venv on the way changes nothing: only a step out taken by a link that sits
    in a dependency folder or a venv is kept."""
    secret = _outside_secret(tmp_path)
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    _make_venv(delegate, tmp_path / "uv-python" / "bin" / "python3.12")
    (delegate / "a" / "b" / "c").mkdir(parents=True)
    (delegate / "a" / "b" / "c" / "up").symlink_to("../../..")
    (delegate / "a" / "b" / "c" / "esc").symlink_to(esc)
    assert (delegate / "a" / "b" / "c" / "esc").read_text() == secret.read_text()  # the chain really reaches it
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    with pytest.raises(RuntimeError, match=r"the result holds a link outside the repository: a/b/c/esc"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def test_a_venv_is_copied_whole_and_its_pycache_removed_from_the_copy_only(
    tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-b: a venv is copied in one `cp` (leaving its `__pycache__` out while
    copying cost one `cp` per entry: 11 s against 1.5 s on a 4,400-file venv),
    then its `__pycache__` folders are removed from the copy, never from the
    clone, and never through a link."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.pyc").write_text("not the copy's")
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    for clone in (keep, delegate):
        venv = _make_venv(clone, tmp_path / "uv-python" / "bin" / "python3.12")
        for folder in ("lib/site-packages/__pycache__", "lib/site-packages/pkg/__pycache__"):
            (venv / folder).mkdir(parents=True)
            (venv / folder / "m.cpython-312.pyc").write_bytes(b"\0" + str(clone).encode())
        (venv / "lib" / "__pycache__").symlink_to(elsewhere, target_is_directory=True)
    copied: list[Path] = []
    real_copy_tree = replay.copy_tree

    def recording(source: Path, target: Path) -> None:
        copied.append(source)
        real_copy_tree(source, target)

    monkeypatch.setattr(replay, "copy_tree", recording)
    _judge_both(tmp_path, snap, keep, delegate)
    assert keep / ".venv" in copied and delegate / ".venv" in copied
    for letter in ("A", "B"):
        venv = tmp_path / "j" / letter / ".venv"
        assert not os.path.lexists(venv / "lib" / "site-packages" / "__pycache__")
        assert not os.path.lexists(venv / "lib" / "site-packages" / "pkg" / "__pycache__")
        assert os.readlink(venv / "lib" / "__pycache__") == str(elsewhere)
    assert (elsewhere / "keep.pyc").read_text() == "not the copy's"
    assert all((clone / ".venv" / "lib" / "site-packages" / "__pycache__").is_dir() for clone in (keep, delegate))


def _editable_venv(clone: Path) -> None:
    """What `uv sync` leaves for an editable src-layout project `probe`: a real
    virtualenv whose `.pth`, `direct_url.json`, entry script and activate
    scripts name the clone, and a `.pyc` a test run compiled in it. Like uv,
    the `.pth` and `direct_url.json` spell the clone's resolved path."""
    (clone / "src" / "probe").mkdir(parents=True)
    (clone / "src" / "probe" / "__init__.py").write_text("from pathlib import Path\nHERE = Path(__file__).resolve()\n")
    venv = clone / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    (site,) = (venv / "lib").glob("python*/site-packages")
    (site / "_editable_impl_probe.pth").write_text(f"{clone.resolve() / 'src'}\n")
    (site / "probe-0.1.0.dist-info").mkdir()
    (site / "probe-0.1.0.dist-info" / "direct_url.json").write_text(
        json.dumps({"url": f"file://{clone.resolve()}", "dir_info": {"editable": True}})
    )
    (venv / "bin" / "probe").write_text(f"#!{venv / 'bin' / 'python'}\nimport probe\n")
    (site / "helper.py").write_text("X = 1\n")
    subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import helper"], cwd=clone.parent, check=True, env=_plain_env()
    )
    assert list((site / "__pycache__").glob("helper.*.pyc"))


def _plain_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}


def _naming(root: Path, *paths: Path) -> list[str]:
    """The files under `root` naming any of `paths`, raw or percent-encoded."""
    needles = [spelling.encode() for p in paths for spelling in {str(p), quote(str(p), safe="/")}]
    return [
        str(f.relative_to(root))
        for f in root.rglob("*")
        if f.is_file() and not f.is_symlink() and any(n in f.read_bytes() for n in needles)
    ]


def test_a_kept_virtualenv_runs_the_copys_own_code_and_names_no_clone(tmp_path: Path, snap: Path) -> None:
    """F36: uv writes the clone's path into the editable `.pth`,
    `direct_url.json`, entry scripts and activate scripts, so the copy's venv
    imported the clone's code, which can read the clone's restored `.env`.
    Each kept venv is relocated to its copy, its `__pycache__` (compiled with
    the clone's paths) is left out, and nothing under A or B names a clone.
    The clones sit behind a linked folder, so the clone's path as given and
    as resolved (which uv writes) differ, as `/var` and `/private/var` do."""
    (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
    keep, delegate = replay.restore(snap, tmp_path / "linked" / "k"), replay.restore(snap, tmp_path / "linked" / "d")
    for clone in (keep, delegate):
        _editable_venv(clone)
        assert _naming(clone / ".venv", clone) and _naming(clone / ".venv", clone.resolve())  # as uv's venv does
    _judge_both(tmp_path, snap, keep, delegate)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        imported = subprocess.run(
            [str(copy / ".venv" / "bin" / "python"), "-c", "import probe; print(probe.HERE)"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=_plain_env(),
        )
        assert imported.stdout.strip() == str((copy / "src" / "probe" / "__init__.py").resolve()), imported.stderr
        assert (copy / ".venv" / "bin" / "probe").read_text().startswith(f"#!{copy / '.venv' / 'bin' / 'python'}\n")
        assert not list(copy.glob(".venv/lib/python*/site-packages/__pycache__"))
        assert _naming(copy, keep, delegate, keep.resolve(), delegate.resolve()) == []


@pytest.mark.parametrize("spelling", ["raw", "percent-encoded"])
def test_a_venv_file_still_naming_the_clone_refuses_the_judge(tmp_path: Path, snap: Path, spelling: str) -> None:
    """F36, F42: a binary file (one holding a NUL byte) is never rewritten, so
    one that names the clone is still found by the scan, in either spelling
    (a `file://` URL holds the percent-encoded one), and judging is refused.
    The clones sit in a folder with a space, so the two spellings differ."""
    interpreter = tmp_path / "uv-python" / "bin" / "python3.12"
    spaced = tmp_path / "with space"
    keep, delegate = replay.restore(snap, spaced / "k"), replay.restore(snap, spaced / "d")
    for clone in (keep, delegate):
        _make_venv(clone, interpreter)
        name = str(clone) if spelling == "raw" else quote(str(clone), safe="/")
        (clone / ".venv" / "lib" / "native.so").write_bytes(b"\x7fELF\0" + name.encode() + b"\0")
    calls: list[str] = []

    def codex(prompt: str, work: Path) -> str:
        calls.append(prompt)
        return ANSWER

    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    with pytest.raises(RuntimeError, match=r"still names its source clone, in .*native\.so"):
        judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert calls == []


def test_a_percent_encoded_clone_path_is_relocated_to_the_copys(tmp_path: Path, snap: Path) -> None:
    """F42: with a space in the checkout path, an editable install records the
    clone as a percent-encoded `file://` URL in `direct_url.json`. It is
    relocated to the copy's own encoded path, the `.pth` beside it to the raw
    one, and nothing under A or B names a clone in either spelling. The judge's
    folder has a space too, so the copy's two spellings differ as well."""
    spaced = tmp_path / "with space"
    keep, delegate = replay.restore(snap, spaced / "k"), replay.restore(snap, spaced / "d")
    for clone in (keep, delegate):
        site = _make_venv(clone, tmp_path / "uv-python" / "bin" / "python3.12") / "lib" / "site-packages"
        (site / "probe-0.1.0.dist-info").mkdir()
        url = {"url": f"file://{quote(str(clone), safe='/')}", "dir_info": {"editable": True}}
        (site / "probe-0.1.0.dist-info" / "direct_url.json").write_text(json.dumps(url))
        (site / "_editable_impl_probe.pth").write_text(f"{clone / 'src'}\n")
    result = {"sides": {"keep": {"clone": str(keep), "cost": 1.0}, "delegate": {"clone": str(delegate), "cost": 1.0}}}
    judge.judge(snap, result, spaced / "j", codex=lambda prompt, work: ANSWER, rng=random.Random(3))
    for letter in ("A", "B"):
        copy = spaced / "j" / letter
        site = copy / ".venv" / "lib" / "site-packages"
        recorded = json.loads((site / "probe-0.1.0.dist-info" / "direct_url.json").read_text())["url"]
        assert recorded == f"file://{quote(str(copy), safe='/')}"
        assert (site / "_editable_impl_probe.pth").read_text() == f"{copy / 'src'}\n"
        assert _naming(copy, keep, delegate) == []


def test_empty_json_cache_contents_are_never_secrets(tmp_path: Path, repo: Path) -> None:
    """F38, F40: restored `.pytest_cache` files holding `{}`, `[]` or `null`
    (ASCII whitespace aside) are no secrets: restore does not record them, a
    replay's new files holding the same are not refused, and the `{}` in
    `node_modules/.package-lock.json` reaches the judge."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".pytest_cache/\n")
    assert _git(repo, "add", ".gitignore").returncode == 0
    assert _git(repo, "commit", "-qm", "ignore .pytest_cache").returncode == 0
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    trivial = {"lastfailed": "{}", "nodeids": "[]\n", "stepwise": " null\n"}
    for name, text in trivial.items():
        (cache / name).write_text(text)
    snap = _snapshot_of(tmp_path, repo)
    clones = [replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")]
    for clone in clones:
        assert (clone / ".pytest_cache" / "v" / "cache" / "nodeids").read_text() == "[]\n"
        assert list(replay.restored_blobs(clone).values()) == [".env"]
        for name, text in trivial.items():
            (clone / f"{name}.json").write_text(text)
    _judge_both(tmp_path, snap, *clones)
    for letter in ("A", "B"):
        copy = tmp_path / "j" / letter
        assert (copy / "node_modules" / ".package-lock.json").read_text() == "{}"
        assert {name: (copy / f"{name}.json").read_text() for name in trivial} == trivial


@pytest.mark.parametrize(
    "secret",
    [bytes(range(128, 256)), "пароль ключ €\n".encode()],
    ids=["binary-key", "unicode-passphrase"],
)
def test_a_secret_with_no_ascii_letter_or_digit_copied_elsewhere_is_refused(
    tmp_path: Path, repo: Path, secret: bytes
) -> None:
    """F40: only `{}`, `[]`, `null` and the empty file are exempt by their
    content. A restored raw binary key, or a passphrase written only in
    non-ASCII characters, copied to a non-ignored file is refused."""
    (repo / ".env").write_bytes(secret)
    snap = _snapshot_of(tmp_path, repo)
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    assert list(replay.restored_blobs(keep).values()) == [".env"]
    shutil.copy(delegate / ".env", delegate / "key.bin")
    _refused_with_no_codex_call(
        tmp_path, snap, keep, delegate, r"key\.bin in the delegate clone holds the content of .* \.env"
    )


# Final Minor 13: a timed-out judge takes the processes it started down with it.
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    exe = tmp_path / "fake-codex"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    monkeypatch.setenv("JEV_FORK_CHECK_CODEX", str(exe))


def test_a_judge_timeout_kills_codex_and_everything_it_started(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pidfile = tmp_path / "child.pid"
    fake_codex(tmp_path, monkeypatch, f"sleep 60 &\necho $! > {pidfile}\nsleep 60\n")

    class StartsTheClockOnceTheChildRuns(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            deadline = time.monotonic() + 30
            while not pidfile.exists() and self.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            return super().communicate(input, timeout=0.2)

    monkeypatch.setattr(
        judge, "subprocess", types.SimpleNamespace(**{**vars(subprocess), "Popen": StartsTheClockOnceTheChildRuns})
    )
    with pytest.raises(RuntimeError, match="timed out"):
        judge.run_codex("judge this", tmp_path)
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the test process codex started outlived the judge's timeout")


def test_a_codex_failure_names_its_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_codex(tmp_path, monkeypatch, "echo 'model not available' >&2\nexit 3\n")
    with pytest.raises(RuntimeError, match="codex exec failed: model not available"):
        judge.run_codex("judge this", tmp_path)


def point(keep_cost: float, del_cost: float, prefer: str = "tie", delegate_ok: bool = True) -> dict:
    side = {"tests": "pass", "outcome_met": True}
    return {
        "meta": {"id": "x", "created": "2026-10-01T10:00:00+00:00", "median_calls": 20},
        "result": {
            "sides": {
                "keep": {"cost": keep_cost, "wall_seconds": 100, "calls": 20, "delegated": False},
                "delegate": {"cost": del_cost, "wall_seconds": 90, "calls": 22, "delegated": True},
            }
        },
        "verdict": {
            "keep": side,
            "delegate": side if delegate_ok else {"tests": "fail", "outcome_met": True},
            "prefer": prefer,
            "why": "",
        },
    }


def test_check_passes_on_twenty_cheaper_equal_jobs() -> None:
    skipped = [("20261001-090000-s1", "writes to production"), ("20261001-091000-s2", "")]
    inconclusive = [("20261001-092000-i1", "timed out")]
    text, passed = report.check_report([point(1.0, 0.8)] * 20, [], skipped=skipped, inconclusive=inconclusive)
    assert passed
    assert "Skipped by you: 2. Inconclusive: 1." in text
    # Final Minor 11: each skipped and inconclusive job is listed with its reason.
    assert "- 20261001-090000-s1: writes to production" in text
    assert "- 20261001-091000-s2: no reason given" in text
    assert "Inconclusive:\n- 20261001-092000-i1: timed out" in text


def test_a_job_waiting_for_its_verdict_blocks_the_pass() -> None:
    text, passed = report.check_report([point(1.0, 0.8)] * 19, [], [], [], waiting=("20261001-120000-abc",))
    assert not passed
    assert "waiting for 20261001-120000-abc" in text


def test_one_broken_delegate_result_fails_the_check() -> None:
    _, passed = report.check_report([point(1.0, 0.5)] * 19 + [point(1.0, 0.5, delegate_ok=False)], [], [], [])
    assert not passed


def test_period_share_is_reported_without_a_threshold() -> None:
    text, _ = report.check_report([point(1.0, 0.8)] * 20, [], [], [], period_cost=40.0)
    assert "Saving as a share of the period's decided messages (their main-thread cost): 10%" in text


def test_jev_overhead_counts_against_delegation() -> None:
    events = [{"version": 3, "ts": "2026-10-01T10:00:00+00:00", "cost": 1.5, "latency_ms": 0}]
    _, passed = report.check_report([point(1.0, 0.85)] * 20, events, [], [])
    assert not passed  # 17.0 + 1.5 > 0.9 * 20


def test_the_period_starts_when_capture_started_if_known() -> None:
    """Ruling F8: Jev's calls before the first scored job are part of the check."""
    early = {"version": 3, "ts": "2026-10-01T08:30:00+00:00", "cost": 1.0}
    during = {"version": 3, "ts": "2026-10-01T10:00:30+00:00", "cost": 0.5}
    before_capture = {"version": 3, "ts": "2026-10-01T07:00:00+00:00", "cost": 9.0}
    events = [before_capture, early, during]
    points = [point(1.0, 0.8)]  # captured at 10:00
    assert report.period_events(events, points) == [during]
    assert report.period_events(events, points, "2026-10-01T08:00:00+00:00") == [early, during]
    # N3: the earlier of the two starts the period, so a capture_started set after
    # the first scored job never drops the calls in between.
    assert report.period_events(events, points, "2026-10-01T10:30:00+00:00") == [during]
    between = {"version": 3, "ts": "2026-10-01T10:15:00+00:00", "cost": 0.1}
    later_points = [point(1.0, 0.8), {**point(1.0, 0.8), "meta": {"id": "y", "created": "2026-10-01T11:00:00+00:00"}}]
    assert report.period_events([between], later_points, "2026-10-01T10:30:00+00:00") == [between]
    # check_report counts the same period: 0.8 + 1.0 + 0.5 delegate against 1.0 keep.
    text, _ = report.check_report(points, events, [], [], capture_started="2026-10-01T08:00:00+00:00")
    assert "delegate $2.30 with Jev's cost over the period included" in text
