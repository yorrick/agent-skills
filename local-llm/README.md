# local-llm

Delegate simple, well-bounded tasks ("dumb work") from Claude Code to local LLMs served by [LM Studio](https://lmstudio.ai) — keeping frontier models (subscription-billed) as the orchestrator and your GPU busy with the grunt work.

## Why

Routers/proxies that repoint Claude Code's model endpoint corrupt tool calls and require API-key billing. Subagents can't target a different provider ([anthropics/claude-code#38698](https://github.com/anthropics/claude-code/issues/38698)). The pattern that works: the frontier model composes a briefing and shells out to a local executor, which speaks its own protocol natively.

## Commands

### `/pi-local` — full local agent via [pi](https://github.com/badlogic/pi-mono)

The primary command. Claude composes a per-task system-prompt briefing dynamically and delegates to `pi -p` pointed at LM Studio, controlling exactly which pi tools the local model gets via an explicit allowlist:

- default (scout mode): `--tools read,bash` — explore and report; pi's edit/write tools are off (bash could still write in principle — the briefing instructs report-only; use `--tools read` for mechanically read-only)
- `--write`: expands to `--tools read,bash,edit,write` (Claude shows `git diff --stat` afterward)
- `--tools <list>`: your own allowlist, e.g. `--tools read` for a no-bash pure reader

pi discovers `AGENTS.md`/`CLAUDE.md` in the target repo itself, so project conventions ride along for free.

Requires: `pi` CLI with an `lmstudio` provider in `~/.pi/agent/models.json`, e.g.:

```json
{
  "providers": {
    "lmstudio": {
      "baseUrl": "http://localhost:1234/v1",
      "api": "openai-completions",
      "apiKey": "lm-studio",
      "models": [
        { "id": "qwen3-coder-30b-a3b-instruct-mlx", "name": "Qwen 3 Coder 30B A3B", "reasoning": false, "input": ["text"], "contextWindow": 262144, "maxTokens": 32768 }
      ]
    }
  }
}
```

Usage:

```
/pi-local list the test functions in chat_app/tests and what each verifies
/pi-local --model 122b find architectural issues in the websocket layer
/pi-local --write add docstrings to chat_app/main.py
```

### `/ask-local` — bounded read-only agent loop

A dependency-free fallback (stdlib-only Python, no pi required): a hard-budgeted explore-and-answer loop with `read_file`/`list_dir`/`grep` tools, per-file truncation, binary refusal, and token accounting. Use when you want guaranteed read-only behavior with mechanical caps.

Forked from [alisorcorp/ask-local](https://github.com/alisorcorp/ask-local) (MIT). Local modifications: `--context` flag (Claude composes the system-prompt briefing per delegation), default model set to `qwen3-coder-30b-a3b-instruct-mlx`, grep symlink containment, per-read budget enforcement, and truncation clamping.

### `scripts/query_lm.py` — one-shot utility

Not wired to a command: a minimal prompt-in/answer-out helper (no agent loop, no file tools) for quick direct queries or shell pipelines: `uv run --script scripts/query_lm.py "your prompt"` (also accepts piped stdin).

## Model strategy

- `qwen3-coder-30b-a3b` (3B active): default workhorse — boilerplate, docstrings, simple tests, JSON transforms, summaries. Keep tasks short and self-contained.
- `qwen3.5-122b-a10b` (10B active): escalation tier for multi-step tool-calling/reasoning.
- One direction only: local drafts, frontier reviews — not the reverse.
