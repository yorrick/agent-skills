#!/usr/bin/env python3
"""A stand-in for `claude --resume ID --fork-session -p --output-format json
--permission-mode bypassPermissions --model M -- PROMPT`: forks the session file, appends the
prompt and one model call, and prints the JSON result.

Env vars the tests use to steer it:
  JEV_ROUTER_NOTE_FILE      hands the job to the named helper and writes a subagent transcript.
  FAKE_CLAUDE_COLD_WARMUP   every warm-up call is cold: no priced call at all.
  FAKE_CLAUDE_COLD_WARMUP_ONCE=<path>  only the first warm-up call ever seen (tracked in the
                            named counter file) is cold; later ones are warm.
  FAKE_CLAUDE_COLD_JOB      the warm-up is normally priced and warm, but the job call's own
                            first read misses the cache (it is still priced, just not warm).
  FAKE_CLAUDE_DIRTY_WARMUP  a warm-up call leaves a stray file in the working copy.
  FAKE_CLAUDE_CRASH         the job call (never the warm-up) exits non-zero before writing
                            anything: a hard crash with nothing to scan.
  FAKE_CLAUDE_ERROR_RESULT  the job call writes its session file as usual (so a leak in it is
                            still there to find), then reports is_error instead of exiting non-zero:
                            a graceful crash, the kind whose output still names a session id.
  FAKE_CLAUDE_UNPRICED      the job call's model is one `prices.json` has no entry for.
  FAKE_CLAUDE_LEAK_PATH=<path>  the job call's tool input names this path, simulating an
                            incomplete path rewrite.
  FAKE_CLAUDE_SLEEP_CHILD=<path>  spawns a detached `sleep 60`, writes its pid to the named
                            file, then sleeps itself, for exercising the timeout/process-group
                            kill. Short-circuits everything else.
  FAKE_CLAUDE_LOG           appends each call's details to a file.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

args = sys.argv[1:]
sid = args[args.index("--resume") + 1]
prompt = args[args.index("--") + 1]
home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
pdir = home / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", os.getcwd())
warm = prompt.startswith("Reply with the single word")
note = os.environ.get("JEV_ROUTER_NOTE_FILE")


def log(**extra: object) -> None:
    if path := os.environ.get("FAKE_CLAUDE_LOG"):
        with open(path, "a") as handle:
            entry = {"args": args, "cwd": os.getcwd(), "note": note, "router": os.environ.get("JEV_ROUTER"), **extra}
            handle.write(json.dumps(entry) + "\n")


if pidfile := os.environ.get("FAKE_CLAUDE_SLEEP_CHILD"):
    child = subprocess.Popen(["sleep", "60"])
    Path(pidfile).write_text(str(child.pid))
    time.sleep(60)
    sys.exit(0)  # never reached in the test: the parent kills the whole group first

if not warm and os.environ.get("FAKE_CLAUDE_CRASH") == "1":
    log(crashed=True)
    sys.exit(1)

entries = [json.loads(line) for line in (pdir / f"{sid}.jsonl").read_text().splitlines()]
new = str(uuid.uuid4())
# Reason: a real headless prompt carries no `origin`; only an interactive session's
# typed message does.
entries.append({"type": "user", "sessionId": new, "message": {"content": prompt}})

cold_warmup = warm and os.environ.get("FAKE_CLAUDE_COLD_WARMUP") == "1"
if warm and (once := os.environ.get("FAKE_CLAUDE_COLD_WARMUP_ONCE")):
    counter = Path(once)
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    cold_warmup = cold_warmup or seen == 0

if warm and os.environ.get("FAKE_CLAUDE_DIRTY_WARMUP") == "1":
    Path("dirty.txt").write_text("a warm-up should never leave this behind\n")

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
if not warm and (leak_path := os.environ.get("FAKE_CLAUDE_LEAK_PATH")):
    content.append({"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": f"{leak_path}/app.py"}})

if not cold_warmup:
    model_name = "unknown-model" if not warm and os.environ.get("FAKE_CLAUDE_UNPRICED") == "1" else "claude-opus-5-5"
    cold_job = not warm and os.environ.get("FAKE_CLAUDE_COLD_JOB") == "1"
    ctx = 100_000
    entries.append(
        {
            "type": "assistant",
            "sessionId": new,
            "requestId": f"r-{new}",
            "message": {
                "model": model_name,
                "content": content,
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 0 if cold_job else ctx,
                    "cache_creation_input_tokens": ctx if cold_job else 200,
                    "output_tokens": 50,
                },
            },
        }
    )
(pdir / f"{new}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
if not warm:
    Path("RESULT.txt").write_text(f"done by {'delegate' if note else 'keep'}\n")
log()
result: dict = {"type": "result", "session_id": new, "result": "ok"}
if not warm and os.environ.get("FAKE_CLAUDE_ERROR_RESULT") == "1":
    result["is_error"] = True
print(json.dumps(result))
