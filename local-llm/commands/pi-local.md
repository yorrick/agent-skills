---
description: Delegate a task to the local LM Studio model via the pi agent. Claude composes the system-prompt briefing dynamically and controls exactly which pi tools the local model may use (read/bash scout allowlist by default; --write expands it; --tools overrides it).
argument-hint: [--model MODEL] [--tools LIST] [--write] [--think] <task>
allowed-tools: Bash(pi -p --provider lmstudio:*)
---

The user wants to delegate a task to the local LM Studio model through the pi agent. The task description (and any flags) is: $ARGUMENTS

Parse the arguments:
- Recognized flags, which may appear anywhere: `--model` (consumes next token), `--tools` (consumes next token), `--write` (boolean), `--think` (boolean). Everything else, in original order, is the task description.
- Default model: `lmstudio/qwen3-coder-30b-a3b-instruct-mlx` (fast workhorse). Escalation tier: `lmstudio/qwen3.5-122b-a10b` — use it when the user passes `--model 122b` (expand to the full ID), names it explicitly, or the task clearly needs multi-step reasoning.
- `--think` only makes sense with the 122b model (the coder model is non-reasoning): translate it to `--thinking medium`; otherwise pass `--thinking off`.

Resolve the tool allowlist (explicit allowlist, never a denylist — new pi tools stay off unless granted):
- If the user passed `--tools`, use their list verbatim (e.g. `--tools read` for a no-bash pure reader).
- Else if the user passed `--write`: `read,bash,edit,write`.
- Else (scout mode, the default): `read,bash` — the local model explores and reports. Note: pi's edit/write tools are off, but bash itself could still modify files — the briefing instruction is the guardrail there. For mechanically read-only delegation, use `--tools read` (no directory listing/grep then — name the files in the task).

Compose the system-prompt briefing — YOU write it dynamically for every delegation, never reuse a canned one:
- The local model receives NOTHING from this session automatically. pi discovers AGENTS.md/CLAUDE.md in the target repo itself, so do NOT restate what those files already say.
- Write 2-6 sentences covering: (a) a role tailored to this specific task, (b) session context pi cannot discover from files (decisions from this conversation, constraints, what the user cares about right now), and (c) expected output format and audience. Under ~150 words. Never paste file contents — name paths instead.
- In scout mode, include one line telling the model its tools are read-only and it must report rather than modify.

Prerequisites: the `pi` CLI with an `lmstudio` provider configured in `~/.pi/agent/models.json`, and LM Studio serving on localhost:1234.

Then run (from the project directory):

```bash
pi -p --provider lmstudio --model "<MODEL_ID>" --tools <ALLOWLIST> --thinking <LEVEL> \
  --append-system-prompt "<briefing>" \
  "<task description>"
```

Mode rules:
- After a `--write` run completes, ALWAYS run `git diff --stat` and summarize what the local model changed so the user can review; recommend `git diff` review before committing.
- Use a Bash timeout of at least 300000 ms; first call after a model switch may JIT-load the model in LM Studio (~20s extra).

Present pi's final answer to the user. If pi exits non-zero or reports the provider/model is unavailable, relay the error verbatim and do not retry automatically — LM Studio may not be running.
