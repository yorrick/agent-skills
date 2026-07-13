# local-llm

Delegate simple, well-bounded tasks ("dumb work") from Claude Code to local LLMs served by [LM Studio](https://lmstudio.ai) — keeping frontier models (subscription-billed) as the orchestrator and your GPU busy with the grunt work.

## Why

Routers/proxies that repoint Claude Code's model endpoint corrupt tool calls and require API-key billing. Subagents can't target a different provider ([anthropics/claude-code#38698](https://github.com/anthropics/claude-code/issues/38698)). The pattern that works: the frontier model composes a briefing and shells out to a local executor, which speaks its own protocol natively. Rather than maintaining a custom agent loop, this plugin delegates to the [pi](https://github.com/badlogic/pi-mono) agent — a maintained harness with its own tools, session logs, and AGENTS.md/CLAUDE.md discovery.

## `/pi-local`

Claude composes a per-task system-prompt briefing dynamically and delegates to `pi -p` pointed at LM Studio, controlling exactly which pi tools the local model gets via an explicit allowlist:

- default (scout mode): `--tools read,bash` — explore and report; pi's edit/write tools are off (bash could still write in principle — the briefing instructs report-only; use `--tools read` for mechanically read-only)
- `--write`: expands to `--tools read,bash,edit,write` (Claude shows `git diff --stat` afterward)
- `--tools <list>`: your own allowlist, e.g. `--tools read` for a no-bash pure reader

pi discovers `AGENTS.md`/`CLAUDE.md` in the target repo itself, so project conventions ride along for free. For one-shot prompts without any tools, plain `pi -p --no-tools "prompt" --provider lmstudio` covers it — no extra scripts needed.

Requires: the `pi` CLI with an `lmstudio` provider in `~/.pi/agent/models.json`, e.g.:

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
/pi-local --tools read summarize what main.py does
/pi-local --write add docstrings to chat_app/main.py
```

## Model strategy

- `qwen3-coder-30b-a3b` (3B active): default workhorse — boilerplate, docstrings, simple tests, JSON transforms, summaries. Keep tasks short and self-contained.
- `qwen3.5-122b-a10b` (10B active): escalation tier for multi-step tool-calling/reasoning.
- One direction only: local drafts, frontier reviews — not the reverse.
