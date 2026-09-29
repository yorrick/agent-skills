"""Shadow mode's decisions against what the turns really did."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402
import jev_router  # noqa: E402
import report  # noqa: E402
import usage  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def event(transcript: Path, prompt: str, outcome: str = "delegate") -> dict:
    return {
        "ts": "2026-10-01T10:00:00+00:00",
        "harness": "claude",
        "version": 3,
        "outcome": outcome,
        "transcript_path": str(transcript),
        "prompt_sha": usage.prompt_sha(prompt),
        "context": 400_000,
        "model": "claude-opus-5-5",
        "helper_model": "claude-opus-5-5",
        "added": 3_000,
        "output": 1_000,
        "steps": 3.2,
        "median_calls": 30,
        "answered": True,
        "cost": 0.00002,
        "latency_ms": 300,
    }


def test_turn_runs_from_the_message_to_the_next_typed_one() -> None:
    entries = [typed("a"), assistant("r1"), typed("b"), assistant("r2"), assistant("r3"), typed("c")]
    turn = report.turn_after(entries, usage.prompt_sha("b"))
    assert turn is not None and len(usage.calls(turn)) == 2


def test_a_repeated_prompt_finds_the_turn_nearest_in_time() -> None:
    first = {**typed("continue"), "timestamp": "2026-10-01T09:00:00Z"}
    second = {**typed("continue"), "timestamp": "2026-10-01T10:00:00Z"}
    entries = [first, assistant("r1"), second, assistant("r2"), assistant("r3")]
    turn = report.turn_after(entries, usage.prompt_sha("continue"), near="2026-10-01T10:00:01+00:00")
    assert turn is not None and len(usage.calls(turn)) == 2


def test_rows_join_decisions_with_real_calls_and_cost(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    entries = [typed("build"), *[assistant(f"r{i}", read=400_000) for i in range(25)], typed("next")]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    (row,) = report.shadow_rows([event(transcript, "build")], usage.load_prices())
    assert row["real_calls"] == 25
    assert row["real_cost"] > 0
    assert row["would_lose"] is False


def test_report_counts_decisions_and_jev_overhead(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(typed("x")) + "\n")
    events = [event(transcript, "x"), event(transcript, "y", outcome="keep"), {"version": 3, "outcome": "no_session"}]
    text = report.shadow_report(events, usage.load_prices())
    assert "Messages seen: 3; decided: 2; worth a fresh subagent: 1 (50%)." in text
    assert "Jev: 2 answered calls, $0.0000, 0.6 s of added wait in total." in text


def test_root_matches_jev_routers_fork_check_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ROUTER_HOME", str(tmp_path))
    assert fork_check.root() == jev_router.fork_check_dir(jev_router.load_config())
    assert fork_check.root() == tmp_path / "fork-check"


def test_snapshot_dirs_skips_tmp_folders_even_with_meta_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_ROUTER_HOME", str(tmp_path))
    snapshots = fork_check.root() / "snapshots"
    done = snapshots / "abc123"
    done.mkdir(parents=True)
    (done / "meta.json").write_text("{}")
    abandoned = snapshots / "def456.tmp"
    abandoned.mkdir(parents=True)
    (abandoned / "meta.json").write_text("{}")
    assert fork_check.snapshot_dirs() == [done]


def test_main_shadow_prints_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JEV_ROUTER_HOME", str(tmp_path))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(typed("x")) + "\n")
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(event(transcript, "x")) + "\n")
    assert fork_check.main(["shadow"]) == 0
    assert "# Shadow report" in capsys.readouterr().out


# --- Ruling T13c: cmd_report selects by status history, not the latest status ---


def _snap(sid: str, created: str) -> Path:
    d = fork_check.root() / "snapshots" / sid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"id": sid, "created": created, "median_calls": 20}))
    return d


def _result(sid: str, **overrides: object) -> Path:
    out = fork_check.root() / "results" / sid
    out.mkdir(parents=True)
    result = {
        "id": sid,
        "sides": {
            "keep": {"cost": 1.0, "wall_seconds": 100, "calls": 20, "delegated": False},
            "delegate": {"cost": 0.8, "wall_seconds": 90, "calls": 22, "delegated": True},
        },
    }
    result.update(overrides)
    (out / "result.json").write_text(json.dumps(result))
    return out


def _verdict(out: Path) -> None:
    side = {"tests": "pass", "outcome_met": True}
    (out / "verdict.json").write_text(json.dumps({"keep": side, "delegate": side, "prefer": "tie", "why": ""}))


def test_cmd_report_selects_ever_safe_jobs_in_capture_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job counts by its status HISTORY (ever marked safe), not just its latest
    status: a publish that later fails does not undo a job the judge already
    scored (a), a failed replay is waiting rather than silently dropped (b), and a
    trial job is excluded outright even though it too was marked safe (c)."""
    monkeypatch.setenv("JEV_ROUTER_HOME", str(tmp_path))
    _snap("20261001-090000-a", "2026-10-01T09:00:00+00:00")
    _snap("20261001-093000-b", "2026-10-01T09:30:00+00:00")
    _snap("20261001-100000-c", "2026-10-01T10:00:00+00:00")
    _snap("20261001-103000-d", "2026-10-01T10:30:00+00:00")

    out_a = _result("20261001-090000-a")
    _verdict(out_a)
    fork_check.set_status("20261001-090000-a", "safe")
    fork_check.set_status("20261001-090000-a", "judged", "tie")
    fork_check.set_status("20261001-090000-a", "publish_failed", "network down")

    fork_check.set_status("20261001-093000-b", "safe")
    fork_check.set_status("20261001-093000-b", "replay_failed", "boom")

    _result("20261001-100000-c", trial=True)
    fork_check.set_status("20261001-100000-c", "safe")
    fork_check.set_status("20261001-100000-c", "replayed", "")

    out_d = _result("20261001-103000-d")
    _verdict(out_d)
    fork_check.set_status("20261001-103000-d", "safe")
    fork_check.set_status("20261001-103000-d", "judged", "tie")

    assert fork_check.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "Jobs judged: 2 of 20, waiting for 20261001-093000-b" in out
    text = (fork_check.root() / "report.md").read_text()
    assert "20261001-090000-a" in text
    assert "20261001-103000-d" in text
    assert "20261001-100000-c" not in text
