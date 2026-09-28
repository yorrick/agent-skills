---
name: jev
description: Switch the Jev model router on or off, or show its status. Use when the user types /jev, $jev, "jev on", "jev off", "jev status", asks to turn the Jev router on or off, asks how many messages went to each size, or asks what Jev has cost. The router asks Jev (TypeSafe's model on OpenRouter) how big each message is and sends small jobs to a smaller, cheaper model. It is off until the user turns it on.
---

# Jev router switch

Run the router script in this skill's `scripts/` directory with the user's word: `on`, `off` or `status` (no word means `status`).

```bash
uv run --quiet --script <this skill's directory>/scripts/jev_router.py status
```

Show the user the script's output as it is. It already carries the privacy reminder when the router is on. Do nothing else.

- `on` needs, once, the shell file that exports `OPENROUTER_API_KEY`: add `--key-file <path>`. The path is remembered for later `on`s. If `on` fails because no key file is known, ask the user for the path. Never search for keys and never print one.
- `classify "<message>" ...` sizes messages without routing anything and prints a table of what Jev picked and how sure it was. Add `--harness codex` or `--harness opencode` to use those harnesses' sizes.

## What the router does

One switch, in `~/.config/jev-router/`, covers Claude Code, Codex and opencode. While it is on, each message the user types is sent to Jev with two typed questions: what is the smallest model that can do this job well, and does the message only make sense inside the conversation? Each size maps to a model and a thinking level inside the same harness (the table is `scripts/tiers.json`). When Jev gives its pick at least a 60% probability, and the message is not a short reply that only makes sense inside the conversation, the job goes to the helper for that size, and the reply ends with a line such as `Done by Claude Opus 5.5 at low thinking`. Headless runs (`claude -p`, `codex exec`, `opencode run`) are never routed. If Jev is slow (over 2 seconds) or anything fails, the message goes through as if the router were not there.

| Harness | How a job reaches its model and thinking level |
|---|---|
| Claude Code | The main model is told to hand the job to the `jev-router:<size>` subagent, unless it is certain it already runs that model at that level. It still reads the message and relays the result. |
| Codex | The main model is told to `spawn_agent` with the size's model and `reasoning_effort`, with the same exception and relay cost. |
| opencode | The message itself moves onto the `jev-<size>` agent, model and thinking level, so there is no relay. |
