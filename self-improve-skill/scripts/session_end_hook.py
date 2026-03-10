#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Self-Improve Plugin — SessionEnd Hook.

Receives JSON via stdin with transcript_path, session_id, and cwd.
Analyzes the transcript to determine if the session was substantial enough
to warrant reflection, then spawns a background `claude -p` process to
analyze learnings and improve project documentation.

Uses Claude Max subscription (not API key) for the background call.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Minimum number of tool uses to consider a session "substantial"
MIN_TOOL_USES = 5

LOG_DIR = Path.home() / ".claude" / ".logs" / "reflect"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE: Path | None = None


def init_log_file(session_id: str, prefix: str = "") -> None:
    global LOG_FILE
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = LOG_DIR / f"{timestamp}_{prefix}{session_id}.log"


def log(message: str) -> None:
    if LOG_FILE is None:
        return
    with LOG_FILE.open("a") as f:
        f.write(message + "\n")


def count_tool_uses(transcript_content: str) -> int:
    """Count tool_use entries in the transcript to gauge session substance."""
    count = 0
    for line in transcript_content.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        if entry.get("type") == "tool_use":
            count += 1
            continue

        message = entry.get("message", {})
        if message.get("role") == "assistant":
            content = message.get("content", [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        count += 1

    return count


def run_reflection(session_id: str, transcript_path: Path, cwd: str) -> None:
    """Spawn a background claude process to reflect on the session.

    Uses Claude Max subscription by unsetting ANTHROPIC_API_KEY.
    Uses Sonnet with high reasoning effort.
    """
    log("Launching background reflection")
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stdout_file = LOG_DIR / f"{timestamp}_{session_id}_reflect.stdout.log"
        stderr_file = LOG_DIR / f"{timestamp}_{session_id}_reflect.stderr.log"

        # Build environment without ANTHROPIC_API_KEY so claude uses Max subscription
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        with (
            transcript_path.open("r") as stdin_f,
            stdout_file.open("w") as stdout_f,
            stderr_file.open("w") as stderr_f,
        ):
            subprocess.Popen(
                [
                    "claude",
                    "--cwd",
                    cwd,
                    "--model",
                    "claude-sonnet-4-6",
                    "--reasoning-effort",
                    "high",
                    "--permission-mode",
                    "bypassPermissions",
                    "-p",
                    "/self-improve-skill:reflect --non-interactive",
                ],
                stdout=stdout_f,
                stderr=stderr_f,
                stdin=stdin_f,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        log("Background reflection launched")
        log(f"  cwd: {cwd}")
        log(f"  transcript: {transcript_path}")
        log(f"  stdout: {stdout_file}")
        log(f"  stderr: {stderr_file}")
    except FileNotFoundError:
        log("claude command not found — is it installed and in PATH?")
    except Exception as e:
        log(f"Error launching reflection: {e}")


def reflect_on_transcript(transcript_path: Path, session_id: str, cwd: str) -> None:
    """Check if a transcript is substantial enough and launch reflection."""
    if not transcript_path.is_file():
        log(f"Transcript file not found: {transcript_path}")
        return

    transcript_content = transcript_path.read_text()
    tool_count = count_tool_uses(transcript_content)
    log(f"Tool uses in session: {tool_count}")

    if tool_count < MIN_TOOL_USES:
        log(f"Session too short ({tool_count} < {MIN_TOOL_USES} tool uses) — skipping reflection")
        return

    run_reflection(session_id, transcript_path, cwd)
    print(f"Reflection launched for session {session_id} ({tool_count} tool uses)")


def main() -> None:
    raw_input = sys.stdin.read()

    try:
        data = json.loads(raw_input)
        transcript_path = Path(data.get("transcript_path", ""))
        session_id = data.get("session_id", "unknown")
        cwd = data.get("cwd", str(Path.cwd()))
    except json.JSONDecodeError:
        print("Failed to parse JSON input")
        return

    init_log_file(session_id)
    log(f"=== SessionEnd hook triggered at {datetime.now()} ===")
    log(f"Session ID: {session_id}")
    log(f"CWD: {cwd}")
    log(f"Transcript path: {transcript_path}")

    reflect_on_transcript(transcript_path, session_id, cwd)


if __name__ == "__main__":
    main()
