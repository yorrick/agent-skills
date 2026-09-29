#!/usr/bin/env python3
"""A stand-in for `claude --resume ID --fork-session -p PROMPT`: forks the session file, appends the
prompt and one model call, and prints the JSON result. With JEV_ROUTER_NOTE_FILE set it hands the
job to the named helper and writes a subagent transcript. FAKE_CLAUDE_COLD=1 makes a non-warm-up
call read nothing from cache. FAKE_CLAUDE_LOG appends each call's details to a file."""

import json
import os
import re
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
sid, prompt = args[args.index("--resume") + 1], args[args.index("-p") + 1]
home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
pdir = home / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", os.getcwd())
entries = [json.loads(line) for line in (pdir / f"{sid}.jsonl").read_text().splitlines()]
new = str(uuid.uuid4())
warm = prompt.startswith("Reply with the single word")
cold = os.environ.get("FAKE_CLAUDE_COLD") == "1" and not warm
note = os.environ.get("JEV_ROUTER_NOTE_FILE")
entries.append({"type": "user", "sessionId": new, "origin": {"kind": "human"}, "message": {"content": prompt}})
content: list[dict] = [{"type": "text", "text": "ok"}]
if note and not warm:
    helper = re.search(r"`(jev-router:[a-z]+)`", Path(note).read_text())
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "Agent",
            "input": {"subagent_type": helper.group(1) if helper else "?", "prompt": "brief"},
        }
    ]
    sub = pdir / new / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "isSidechain": True,
                "requestId": "s1",
                "message": {
                    "model": "claude-opus-5-5",
                    "content": [],
                    "usage": {
                        "input_tokens": 5,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 25_000,
                        "output_tokens": 500,
                    },
                },
            }
        )
        + "\n"
    )
ctx = 100_000
entries.append(
    {
        "type": "assistant",
        "sessionId": new,
        "requestId": f"r-{new}",
        "message": {
            "model": "claude-opus-5-5",
            "content": content,
            "usage": {
                "input_tokens": 5,
                "cache_read_input_tokens": 0 if cold else ctx,
                "cache_creation_input_tokens": ctx if cold else 200,
                "output_tokens": 50,
            },
        },
    }
)
(pdir / f"{new}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
if not warm:
    Path("RESULT.txt").write_text(f"done by {'delegate' if note else 'keep'}\n")
if log := os.environ.get("FAKE_CLAUDE_LOG"):
    with open(log, "a") as handle:
        handle.write(
            json.dumps({"args": args, "cwd": os.getcwd(), "note": note, "router": os.environ.get("JEV_ROUTER")}) + "\n"
        )
print(json.dumps({"type": "result", "session_id": new, "result": "ok"}))
