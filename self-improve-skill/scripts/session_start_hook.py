#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Self-Improve Plugin — SessionStart Hook (workaround for /clear).

When source is "clear", SessionEnd doesn't fire (known bug).
This hook catches that case by finding the previous session's transcript
and reflecting on it.

Only runs when source is "clear" — ignores "startup", "resume", "compact".
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Import shared logic from the session_end_hook
# Since both scripts are in the same directory, we can import directly
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from session_end_hook import init_log_file, log, reflect_on_transcript


def find_previous_transcript(current_transcript: Path) -> Path | None:
    """Find the most recent transcript that isn't the current session's.

    Transcripts are JSONL files in the same directory as the current transcript.
    """
    transcript_dir = current_transcript.parent
    if not transcript_dir.is_dir():
        return None

    candidates = sorted(
        (f for f in transcript_dir.glob("*.jsonl") if f != current_transcript),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    return candidates[0] if candidates else None


def main() -> None:
    raw_input = sys.stdin.read()

    try:
        data = json.loads(raw_input)
        source = data.get("source", "")
        session_id = data.get("session_id", "unknown")
        transcript_path = Path(data.get("transcript_path", ""))
        cwd = data.get("cwd", str(Path.cwd()))
    except json.JSONDecodeError:
        return

    # Only handle /clear — other sources are handled by SessionEnd or don't need reflection
    if source != "clear":
        return

    init_log_file(session_id, prefix="clear_")
    log(f"=== SessionStart (clear) hook triggered at {datetime.now()} ===")
    log(f"Session ID: {session_id}")
    log(f"CWD: {cwd}")
    log(f"Current transcript: {transcript_path}")

    previous = find_previous_transcript(transcript_path)
    if previous is None:
        log("No previous transcript found — skipping")
        return

    log(f"Previous transcript: {previous}")
    # Extract session ID from the transcript filename (UUID.jsonl)
    prev_session_id = previous.stem
    reflect_on_transcript(previous, prev_session_id, cwd)


if __name__ == "__main__":
    main()
