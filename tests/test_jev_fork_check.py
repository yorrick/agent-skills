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


@pytest.mark.parametrize(
    "command",
    [
        "git -C /work/app push origin HEAD",
        "git -C '/work/my app' push",
        "git -c push.default=current push",
        "http example.org/api/items name=widget",
        "https api.example.org/items count:=3",
        "http --form :8000/upload file@report.pdf name=x",
        "curl -d 'a=1' https://example.org/api",
        "curl --data @body.json https://example.org/api",
        "curl --json '{}' https://example.org/api",
        "curl -X PUT https://example.org/api/1",
        "curl --request DELETE https://example.org/api/1",
        "gh api --method PATCH repos/acme/shop",
        "gh api -X POST repos/acme/shop/issues",
        "gh api repos/acme/shop/issues -f title=bug",
        "aws s3 cp build s3://bucket/ --recursive",
        "cd infra && AWS_PROFILE=prod aws s3 ls",
        "make dist | aws s3 cp - s3://bucket/dist.tar",
        "uv run pytest\naws lambda invoke --function-name x out.json",
    ],
)
def test_external_actions_catch_the_common_writing_forms(command: str) -> None:
    """Final Minor 9: each of these reaches outside the machine."""
    assert fork_check.external_actions([tool("Bash", command=command)]) == [f"shell: {command}"]


@pytest.mark.parametrize(
    "command",
    [
        "git -C /work/app status",
        "http example.org/api/items q==widget",
        "http GET example.org/api/items name=x",
        "python -m http.server 8000",
        "curl https://example.org/api",
        "curl -X GET https://example.org/api",
        "gh api repos/acme/shop",
        "gh api -X GET search/issues -f q=bug",
        "gh api --method=get repos/acme/shop",
        # N4: curl's flags are case-sensitive, and aws counts only as a command.
        "curl -D headers.txt https://example.org/api",
        "curl -f https://example.org/api",
        "curl -x http://proxy:8080 https://example.org/api",
        "git commit -m 'bump AWS region'",
        "grep -rn aws src/",
    ],
)
def test_external_actions_leave_reads_alone(command: str) -> None:
    assert fork_check.external_actions([tool("Bash", command=command)]) == []


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
                "prompt_cut": False,
            }
        )
    )

    assert fork_check.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "20261001-100000-abc" in out
    assert "shell: git push origin HEAD" in out
    assert "abandoned" not in out
    # Ledger, Task 11: whether the snapshot cut the message out of its transcript.
    assert "| prompt cut |" in out
    assert "| 20261001-100000-abc | app | $1.20 | new | no | shell: git push origin HEAD |" in out


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
