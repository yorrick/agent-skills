---
description: Delegate a task to the local LM Studio model via the pi agent (read/bash tools; add --write for edit/write). Claude composes the system-prompt briefing; pi picks up AGENTS.md/CLAUDE.md itself.
argument-hint: [--model MODEL] [--write] [--think] <task>
allowed-tools: Bash(pi:*)
---

The user wants to delegate a task to the local LM Studio model through the pi agent. The task description (and any flags) is: $ARGUMENTS

Parse the arguments:
- Recognized flags, which may appear anywhere: `--model` (consumes next token), `--write` (boolean), `--think` (boolean). Everything else, in original order, is the task description.
- Default model: `lmstudio/qwen3-coder-30b-a3b-instruct-mlx` (fast workhorse). Escalation tier: `lmstudio/qwen3.5-122b-a10b` — use it when the user passes `--model 122b` (expand to the full ID), names it explicitly, or the task clearly needs multi-step reasoning.
- `--think` only makes sense with the 122b model (the coder model is non-reasoning): translate it to `--thinking medium`; otherwise pass `--thinking off`.

Prerequisites: the `pi` CLI (`brew install` / npm, see pi docs) with an `lmstudio` provider configured in `~/.pi/agent/models.json`, and LM Studio serving on localhost:1234.

Compose the system-prompt briefing — YOU write it for every delegation:
- The local model receives NOTHING from this session automatically. pi discovers AGENTS.md/CLAUDE.md in the target repo itself, so do NOT restate what those files already say.
- Write 2-6 sentences covering: (a) a role tailored to the task, (b) session context pi cannot discover from files (decisions from this conversation, constraints, what the user cares about right now), and (c) expected output format and audience. Under ~150 words. Never paste file contents — name paths instead.

Then run (from the project directory):

```bash
pi -p --provider lmstudio --model "<MODEL_ID>" --thinking off \
  --exclude-tools edit,write \
  --append-system-prompt "<briefing>" \
  "<task description>"
```

Mode rules:
- Default is scout mode: `--exclude-tools edit,write` — the local model can read files and run read-only bash but must not modify anything.
- If the user passed `--write`, drop `--exclude-tools edit,write` so pi may edit/write files. After a --write run completes, ALWAYS run `git diff --stat` and summarize what the local model changed so the user can review; recommend `git diff` review before committing.
- Use a Bash timeout of at least 300000 ms; first call after a model switch may JIT-load the model in LM Studio (~20s extra).

Present pi's final answer to the user. If pi exits non-zero or reports the provider/model is unavailable, relay the error verbatim and do not retry automatically — LM Studio may not be running.
