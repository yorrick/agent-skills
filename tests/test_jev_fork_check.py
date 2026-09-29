"""What a real turn did outside the machine, and the user's marks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402


def tool(name: str, **inputs: object) -> dict:
    return {
        "type": "assistant",
        "requestId": name,
        "message": {"content": [{"type": "tool_use", "name": name, "input": inputs}]},
    }


def test_external_actions_name_pushes_prs_deploys_and_mcp_writes() -> None:
    turn = [
        tool("Bash", command="git push origin HEAD"),
        tool("Bash", command="gh pr create --title x --body y"),
        tool("Bash", command="uv run pytest"),
        tool("mcp__econoplus-prod__execute_sql", query="update x"),
        tool("mcp__claude_ai_Gmail__get_message", id="1"),
        tool("Read", file_path="/x"),
    ]
    found = fork_check.external_actions(turn)
    assert found == [
        "shell: git push origin HEAD",
        "shell: gh pr create --title x --body y",
        "MCP: mcp__econoplus-prod__execute_sql",
    ]


def test_marks_are_recorded_and_the_latest_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    (tmp_path / "snapshots" / "20261001-100000-abc").mkdir(parents=True)
    (tmp_path / "snapshots" / "20261001-100000-abc" / "meta.json").write_text("{}")
    assert fork_check.main(["mark", "20261001-100000-abc", "skip", "--reason", "writes to production"]) == 0
    assert fork_check.main(["mark", "20261001-100000-abc", "safe"]) == 0
    assert fork_check.statuses()["20261001-100000-abc"]["status"] == "safe"


def test_list_shows_the_real_turns_external_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real turn (looked up in the transcript named by meta.json, not the
    snapshot's own frozen copy) is scanned for what it did outside the machine."""
    import json

    from test_jev_usage import typed

    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    transcript = tmp_path / "t.jsonl"
    entries = [
        typed("ship it"),
        {**tool("Bash", command="git push origin HEAD"), "timestamp": "2026-10-01T10:00:01Z"},
        typed("next"),
    ]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))

    snap = tmp_path / "snapshots" / "20261001-100000-abc"
    snap.mkdir(parents=True)
    (snap / "message.txt").write_text("ship it")
    (snap / "meta.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript),
                "created": "2026-10-01T10:00:00+00:00",
                "toplevel": "/work/app",
                "expected_saving": 1.2,
            }
        )
    )

    assert fork_check.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "20261001-100000-abc" in out
    assert "shell: git push origin HEAD" in out
    assert "abandoned" not in out


def test_list_reports_abandoned_tmp_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A folder left by a hook killed mid-snapshot ends in `.tmp`; `list` counts it
    separately at the end so the user knows it is safe to delete, rather than
    silently ignoring it the way `snapshot_dirs` does."""
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    abandoned = tmp_path / "snapshots" / "20261001-090000-def.tmp"
    abandoned.mkdir(parents=True)
    (abandoned / "meta.json").write_text("{}")

    assert fork_check.main(["list"]) == 0
    out = capsys.readouterr().out
    assert f"1 abandoned snapshot folders (*.tmp) under {tmp_path / 'snapshots'}; safe to delete." in out


def test_list_says_nothing_about_abandoned_folders_when_there_are_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    assert fork_check.main(["list"]) == 0
    assert "abandoned" not in capsys.readouterr().out
