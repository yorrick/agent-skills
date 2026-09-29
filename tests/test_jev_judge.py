"""A blind judge, and the fork check's pass or fail."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import judge  # noqa: E402
import replay  # noqa: E402
import report  # noqa: E402

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


# F14: a replay-made link into the fork-check folder would unblind the judge.
def _link_case(tmp_path: Path, snap: Path, target: Path) -> tuple[dict, list[str], Callable[[str, Path], str]]:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    (delegate / ".venv" / "bin").mkdir(parents=True)  # ignored in the fixture repo
    (delegate / ".venv" / "bin" / "python").symlink_to(target)
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


def test_a_link_out_of_the_fork_check_folder_stays(tmp_path: Path, snap: Path) -> None:
    """A uv venv's `python` is an absolute link to the interpreter, outside the folder."""
    result, calls, codex = _link_case(tmp_path, snap, tmp_path / "uv-python" / "bin" / "python3.12")
    verdict = judge.judge(snap, result, tmp_path / "j", codex=codex, rng=random.Random(1))
    assert len(calls) == 1 and verdict["prefer"] in ("keep", "delegate")


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
