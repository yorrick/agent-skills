"""The session as the router sees it: read from the end of the transcript only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import session  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_context_growth_output_and_previous_reply(tmp_path: Path) -> None:
    entries = [
        typed("build it"),
        assistant("r1", read=100_000, w1h=2_000, out=300),
        assistant("r2", read=102_000, w1h=3_000, out=500),
        assistant("r3", read=105_000, w1h=1_000, out=100, text="Done. Tests pass."),
    ]
    s = session.read_session(write(tmp_path / "t.jsonl", entries))
    assert s is not None
    assert s.context == 10 + 105_000 + 1_000
    assert s.added == 2_000  # contexts 102010, 105010, 106010: growth (3000 + 1000) / 2
    assert s.output == 300
    assert s.model == "claude-opus-5-5"
    assert s.previous_reply == "Done. Tests pass."


def test_compaction_clips_negative_growth(tmp_path: Path) -> None:
    entries = [assistant("r1", read=500_000, w1h=0), assistant("r2", read=40_000, w1h=0), assistant("r3", read=42_000)]
    s = session.read_session(write(tmp_path / "t.jsonl", entries))
    assert s is not None
    assert s.context == 10 + 42_000 + 50
    assert s.added == (0 + 2_050) // 2


def test_only_the_last_twenty_calls_count(tmp_path: Path) -> None:
    early = [assistant(f"e{i}", read=1_000 * i, out=10_000) for i in range(30)]
    late = [assistant(f"l{i}", read=100_000, out=100) for i in range(20)]
    s = session.read_session(write(tmp_path / "t.jsonl", early + late))
    assert s is not None
    assert s.output == 100


def test_previous_reply_keeps_the_last_1500_characters(tmp_path: Path) -> None:
    s = session.read_session(write(tmp_path / "t.jsonl", [assistant("r1", text="x" * 1000 + "y" * 1500)]))
    assert s is not None
    assert s.previous_reply == "y" * 1500


def test_a_session_with_no_call_has_no_reading(tmp_path: Path) -> None:
    assert session.read_session(write(tmp_path / "t.jsonl", [typed("hello")])) is None


def test_missing_transcript_has_no_reading(tmp_path: Path) -> None:
    assert session.read_session(tmp_path / "nope.jsonl") is None


def test_only_the_tail_is_read(tmp_path: Path) -> None:
    padding = [typed("x" * 10_000) for _ in range(50)]
    path = write(tmp_path / "t.jsonl", [assistant("old", read=9)] + padding + [assistant("new", read=123)])
    s = session.read_session(path, tail_bytes=20_000)
    assert s is not None
    assert s.context == 10 + 123 + 50
