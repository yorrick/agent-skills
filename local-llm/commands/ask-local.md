---
description: Delegate a task to the local LM Studio model (bounded read-only agent loop — reads/greps project files itself, keeps content out of Claude's context). Flags pass through to agent_lm.py.
argument-hint: [--dir DIR] [--model MODEL] [--context TEXT] [--think] [--max-turns N] [--read-budget N] [--max-read-chars N] [--max-file-bytes N] [--no-stream] [--quiet] <task>
allowed-tools: Bash(uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/agent_lm.py":*)
---

The user wants to delegate a task to the local LM Studio model. The task description (and any flags) is: $ARGUMENTS

Parse the arguments:
- Flags may appear anywhere in `$ARGUMENTS` — at the start, end, or interleaved. Extract all recognized flags regardless of position. Recognized flags: `--dir`, `--model`, `--context`, `--think`, `--max-tokens`, `--max-turns`, `--read-budget`, `--max-read-chars`, `--max-file-bytes`, `--no-stream`, `--quiet`.
- Boolean flags (`--think`, `--no-stream`, `--quiet`) take no value. The others each consume the next token as their value.
- Everything that remains after pulling out the flags (and their values) — in original order — is the task description. Quote the task description as a single argument when passing it to the script.
- If `--dir` is not supplied, default it to the current working directory so the agent can read project files.

Compose the system-prompt briefing (unless the user already supplied `--context`):
- YOU write the `--context` value for every delegation. The local model receives NOTHING from this session automatically — no conversation history, no CLAUDE.md — so this briefing is its only window into what you know.
- Write 2-6 sentences covering: (a) a role tailored to the task (e.g. "You are a security auditor" rather than the generic default), (b) project/session context the model cannot discover from files alone (what the project is, relevant conventions, constraints, decisions already made in the conversation), and (c) the expected output format and audience.
- Do NOT paste file contents into the briefing — the agent reads files itself. Name paths instead.
- Keep it under ~150 words; the local model's context window is small.

Then run:

```bash
uv run --script "${CLAUDE_PLUGIN_ROOT}/scripts/agent_lm.py" --dir <DIR> --context "briefing" [other flags] "task description"
```

Defaults: `--max-turns 15`, `--read-budget 15`, `--max-tokens 6000`, streaming on. The read budget mechanically caps `read_file` calls; `list_dir` and `grep` are free. For tasks that legitimately need to read more than 15 files (e.g. full-site page inventories), raise `--read-budget` explicitly. For tasks that want precise output and no in-progress noise, pass `--quiet`.

The script is self-reporting: it prints a `[qwen: peak N prompt tok @ turn X | M completion across K turn(s)]` footer on stdout after the answer. Present the model's final answer and stats footer verbatim. Do not strip or paraphrase the stats line — the user wants to see it.

The agent loop reads files itself via its own tool calls, so do NOT paste file contents into the task description — just name the files or describe the task. That's the whole point: file content stays out of this conversation's context.

The script does its own model-availability preflight and will exit with a clear error if LM Studio isn't running or the requested model isn't loaded. If you see that, relay the error verbatim and do not retry automatically.
