#!/usr/bin/env python3
"""A stand-in for `claude --resume ID --fork-session -p --output-format json --settings JSON
--permission-mode bypassPermissions --model M -- PROMPT`: forks the session file, appends the
prompt and one model call, and prints the JSON result.

A call is a warm-up when JEV_FORK_CHECK_WARMUP is "1", exactly as the runner marks one; the
argv never tells the two apart, since a warm-up is launched exactly like its job. Each call's
one model turn asks for one tool: `Agent` (handing the job to the helper) when a note is set,
`Bash` otherwise. Like the real CLI before a tool call, it first runs the PreToolUse hook
commands its `--settings` name (here through /bin/sh) and treats them as Claude Code's hook
docs say: a `permissionDecision` of "deny" or an exit code of 2 blocks the tool; any other
failure (non-zero exit, a command that cannot start) blocks it only when the hook sets
`"onFailure": "block"`, and otherwise the tool runs anyway. The tool, when it runs, writes
RESULT.txt (and, for `Agent`, a subagent transcript). Each tool call gets a tool_result entry
shaped like the real ones: `permissionDecision` {"decision": "accept", ...} when it ran,
{"decision": "reject", "source": "hook", ...} with is_error when a hook blocked it.

Env vars the tests use to steer it:
  JEV_FORK_CHECK_WARMUP     "1" marks the call as a warm-up (the FAKE_CLAUDE_* switches below
                            that name the warm-up or the job key off this alone).
  JEV_ROUTER_NOTE_FILE      the call's tool is `Agent`, handing the job to the named helper.
  FAKE_CLAUDE_HOOK_FAILS=<warmup|job>  in that call, every hook command fails to start (as if
                            its interpreter had gone: exit 127) instead of running. A hook with
                            `"onFailure": "block"` then blocks the tool with the real CLI's
                            message: `[<command>]: failed; blocking because onFailure is "block"`.
  FAKE_CLAUDE_OTHER_HOOK_DENIES=<warmup|job>  in that call, a PreToolUse hook of the user's own
                            (not one `--settings` names) denies the tool, as a real job's hooks may.
  FAKE_CLAUDE_PROMPT_RECORDED_AS=<text>  the warm-up records its user entry with this text instead
                            of its prompt, so nothing in its transcript matches the prompt.
  FAKE_CLAUDE_CRASH_BEFORE_RESULT=<warmup|job>  that call records its tool_use, runs (or does not
                            run) its tool as usual, then exits non-zero before recording any
                            tool_result.
  FAKE_CLAUDE_JOB_EXTRA_UNCACHED=<n>  the job's first call also writes n more tokens to the cache
                            (a longer request than the warm-up's, read only in part from cache).
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
  FAKE_CLAUDE_UNPRICED_WARMUP  the same, but for the warm-up call.
  FAKE_CLAUDE_LEAK_PATH=<path>  the job call's tool input names this path, simulating an
                            incomplete path rewrite.
  FAKE_CLAUDE_LEAK_PATH_WARMUP=<path>  the same, but for the warm-up call.
  FAKE_CLAUDE_WRONG_SESSION_ID=<warmup|job>  that call's JSON result names a session id it
                            never wrote, while its real session file is written as usual.
  FAKE_CLAUDE_SLEEP_CHILD=<path>  spawns a detached `sleep 60`, writes its pid to the named
                            file, logs the call (with hung=true), then sleeps itself, for any
                            call. Short-circuits everything else.
  FAKE_CLAUDE_SLEEP_CHILD_JOB=<path>  the same, but only for the job call, so the warm-up
                            succeeds normally first.
  FAKE_CLAUDE_HANG_AFTER_WRITE  the job call writes its session file (and any injected leak) and
                            RESULT.txt as usual, logs the call, then hangs before printing its
                            JSON result: a timeout whose transcript is still there to scan.
  FAKE_CLAUDE_LOG           appends each call's details to a file: its full argv, whether it was
                            a warm-up (and the raw JEV_FORK_CHECK_WARMUP value), its
                            JEV_ROUTER_NOTE_FILE, whether a hook stopped it and whether its
                            tool ran, the names of the
                            CLAUDE* variables it inherited, its HOME and PATH, and a digest of
                            its whole environment except JEV_FORK_CHECK_WARMUP (a digest, so no
                            value of the test runner's own environment is written out).
"""

import hashlib
import json
import os
import re
import shlex
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
warm = os.environ.get("JEV_FORK_CHECK_WARMUP") == "1"
note = os.environ.get("JEV_ROUTER_NOTE_FILE")
env_digest = hashlib.sha256(
    json.dumps(sorted((k, v) for k, v in os.environ.items() if k != "JEV_FORK_CHECK_WARMUP")).encode()
).hexdigest()


def log(**extra: object) -> None:
    if path := os.environ.get("FAKE_CLAUDE_LOG"):
        with open(path, "a") as handle:
            entry = {
                "argv": sys.argv,
                "args": args,
                "warmup": warm,
                "warmup_var": os.environ.get("JEV_FORK_CHECK_WARMUP"),
                "env_digest": env_digest,
                "cwd": os.getcwd(),
                "note": note,
                "router": os.environ.get("JEV_ROUTER"),
                "claude_env": sorted(k for k in os.environ if k.startswith("CLAUDE")),
                "git_env": sorted(k for k in os.environ if k.startswith("GIT_")),
                "home": os.environ.get("HOME"),
                "path": os.environ.get("PATH"),
                **extra,
            }
            handle.write(json.dumps(entry) + "\n")


sleep_pidfile = os.environ.get("FAKE_CLAUDE_SLEEP_CHILD")
if not sleep_pidfile and not warm:
    sleep_pidfile = os.environ.get("FAKE_CLAUDE_SLEEP_CHILD_JOB")
if sleep_pidfile:
    child = subprocess.Popen(["sleep", "60"])
    # Reason: written whole, then renamed into place, so a test that interrupts
    # the call as soon as the pid file exists never reads it empty.
    Path(f"{sleep_pidfile}.tmp").write_text(str(child.pid))
    os.replace(f"{sleep_pidfile}.tmp", sleep_pidfile)
    # Reason: logged only once the pid file exists, so a test can wait for this
    # line before starting a timeout clock that would otherwise race the write.
    log(hung=True)
    time.sleep(60)
    sys.exit(0)  # never reached in the test: the parent kills the whole group first

if not warm and os.environ.get("FAKE_CLAUDE_CRASH") == "1":
    log(crashed=True)
    sys.exit(1)


tool = "Agent" if note else "Bash"


def run_hooks() -> tuple[bool, bool, str]:
    """Run every PreToolUse hook command `--settings` names, through /bin/sh, the way
    Claude Code's hook docs describe, and return (blocked, stopped, reason): whether
    the tool is blocked, whether a hook answered `"continue": false`, and the reason
    a blocked tool's result reports."""
    if "--settings" not in args:
        return False, False, ""
    settings = json.loads(args[args.index("--settings") + 1])
    hook_input = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}})
    call = "warmup" if warm else "job"
    fails = os.environ.get("FAKE_CLAUDE_HOOK_FAILS") == call
    blocked, stopped, reason = False, False, ""
    if os.environ.get("FAKE_CLAUDE_OTHER_HOOK_DENIES") == call:
        blocked, reason = True, "the user's own policy hook denies this tool"
    for group in (settings.get("hooks") or {}).get("PreToolUse") or []:
        for hook in group.get("hooks") or []:
            command = hook["command"]
            if fails:
                code, out, err = 127, "", f"/bin/sh: {shlex.split(command)[0]}: No such file or directory"
            else:
                done = subprocess.run(command, shell=True, input=hook_input, capture_output=True, text=True)
                code, out, err = done.returncode, done.stdout, done.stderr
            if code == 2:
                blocked, reason = True, err.strip()
            elif code != 0:
                # Reason: a failed hook does not block the tool unless it says so; when
                # it does, the result reads as Claude Code 2.1.295 words it.
                if hook.get("onFailure") == "block":
                    detail = err.strip() or "No stderr output"
                    blocked, reason = True, f'[{command}]: failed; blocking because onFailure is "block"\n{detail}'
            elif out.strip():
                try:
                    decision = json.loads(out)
                except json.JSONDecodeError:
                    continue
                specific = decision.get("hookSpecificOutput") or {}
                if specific.get("permissionDecision") == "deny":
                    blocked, reason = True, str(specific.get("permissionDecisionReason") or "")
                stopped = stopped or decision.get("continue") is False
    return blocked, stopped, reason


blocked, stopped, reason = run_hooks()

entries = [json.loads(line) for line in (pdir / f"{sid}.jsonl").read_text().splitlines()]
new = str(uuid.uuid4())
# Reason: a real headless prompt carries no `origin`; only an interactive session's
# typed message does.
recorded = (os.environ.get("FAKE_CLAUDE_PROMPT_RECORDED_AS") if warm else None) or prompt
entries.append({"type": "user", "sessionId": new, "message": {"content": recorded}})

cold_warmup = warm and os.environ.get("FAKE_CLAUDE_COLD_WARMUP") == "1"
if warm and (once := os.environ.get("FAKE_CLAUDE_COLD_WARMUP_ONCE")):
    counter = Path(once)
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    cold_warmup = cold_warmup or seen == 0

if warm and os.environ.get("FAKE_CLAUDE_DIRTY_WARMUP") == "1":
    Path("dirty.txt").write_text("a warm-up should never leave this behind\n")

# Reason: a cold warm-up makes no model call, so it never asks for a tool at all.
tool_ran = not blocked and not cold_warmup
if note:
    helper = re.search(r"`(jev-router:[a-z]+)`", Path(note).read_text())
    tool_input = {"subagent_type": helper.group(1) if helper else "?", "prompt": "brief"}
    content: list[dict] = [{"type": "tool_use", "id": "t1", "name": "Agent", "input": tool_input}]
else:
    content = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo done > RESULT.txt"}}]
if note and tool_ran:
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
leak_path = os.environ.get("FAKE_CLAUDE_LEAK_PATH_WARMUP" if warm else "FAKE_CLAUDE_LEAK_PATH")
if leak_path:
    content.append({"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": f"{leak_path}/app.py"}})

crash_before_result = os.environ.get("FAKE_CLAUDE_CRASH_BEFORE_RESULT") == ("warmup" if warm else "job")
if not cold_warmup:
    unpriced = (not warm and os.environ.get("FAKE_CLAUDE_UNPRICED") == "1") or (
        warm and os.environ.get("FAKE_CLAUDE_UNPRICED_WARMUP") == "1"
    )
    model_name = "unknown-model" if unpriced else "claude-opus-5-5"
    cold_job = not warm and os.environ.get("FAKE_CLAUDE_COLD_JOB") == "1"
    extra_uncached = 0 if warm else int(os.environ.get("FAKE_CLAUDE_JOB_EXTRA_UNCACHED") or 0)
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
                    "cache_creation_input_tokens": (ctx if cold_job else 200) + extra_uncached,
                    "output_tokens": 50,
                },
            },
        }
    )
    # Reason: shaped like the real entries (Claude Code 2.1.295): a tool that ran
    # is accepted, one a hook blocked is rejected with source "hook". A call that
    # crashes before recording any result leaves only its tool_use behind.
    for block in [] if crash_before_result else content:
        if tool_ran:
            result_block = {"tool_use_id": block["id"], "type": "tool_result", "content": "done", "is_error": False}
            extra: dict = {"permissionDecision": {"decision": "accept", "source": "config", "reasonType": "mode"}}
        else:
            text = f"PreToolUse:{block['name']} hook error: {reason}"
            result_block = {"tool_use_id": block["id"], "type": "tool_result", "content": text, "is_error": True}
            extra = {
                "toolUseResult": f"Error: {text}",
                "toolDenialKind": "permission-rule",
                "permissionDecision": {"decision": "reject", "source": "hook", "reasonType": "hook"},
            }
        entries.append(
            {"type": "user", "sessionId": new, "message": {"role": "user", "content": [result_block]}, **extra}
        )
(pdir / f"{new}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
if tool_ran:
    Path("RESULT.txt").write_text(f"done by {'delegate' if note else 'keep'}\n")
log(hook_stopped=stopped, tool_ran=tool_ran)
if crash_before_result:
    sys.exit(1)
if not warm and os.environ.get("FAKE_CLAUDE_HANG_AFTER_WRITE") == "1":
    # Reason: the session file (and any leak in it) is already on disk; this
    # simulates a run the caller's timeout has to kill, not a clean exit.
    time.sleep(60)
result: dict = {"type": "result", "session_id": new, "result": "ok"}
if os.environ.get("FAKE_CLAUDE_WRONG_SESSION_ID") == ("warmup" if warm else "job"):
    result["session_id"] = str(uuid.uuid4())
if not warm and os.environ.get("FAKE_CLAUDE_ERROR_RESULT") == "1":
    result["is_error"] = True
print(json.dumps(result))
