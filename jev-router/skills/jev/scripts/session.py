"""What the router needs from a Claude Code session, read from the transcript's end.

A long session's transcript runs to tens of megabytes, and the hook runs before
every message, so only the last few megabytes are read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import usage

TAIL_BYTES = 4_000_000
WINDOW = 20
PREV_CHARS = 1500


@dataclass(frozen=True)
class Session:
    context: int  # tokens the last main-thread call sent: what the next call re-reads
    model: str
    added: int  # new tokens per call, averaged over the last WINDOW calls
    output: int  # output tokens per call, averaged the same way
    previous_reply: str  # the agent's last text, its last PREV_CHARS characters


def _tail(path: Path, tail_bytes: int) -> list[dict]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - tail_bytes))
        lines = handle.read().split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first line was cut in the middle
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def read_session(path: Path, tail_bytes: int = TAIL_BYTES) -> Session | None:
    """None when there is nothing to go on: no transcript, or no model call yet."""
    try:
        entries = _tail(path, tail_bytes)
    except OSError:
        return None
    recent = usage.calls(entries)[-WINDOW:]
    if not recent:
        return None
    contexts = [c.context for c in recent]
    # Reason: a compaction shrinks the context; that is not negative growth.
    growth = [max(0, after - before) for before, after in zip(contexts, contexts[1:])]
    previous = ""
    for entry in entries:
        previous = usage.assistant_text(entry) or previous
    return Session(
        context=contexts[-1],
        model=recent[-1].model,
        added=sum(growth) // len(growth) if growth else 0,
        output=sum(c.out for c in recent) // len(recent),
        previous_reply=previous[-PREV_CHARS:],
    )
