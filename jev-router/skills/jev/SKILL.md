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

One switch, in `~/.config/jev-router/`, covers Claude Code, Codex and opencode. While it is on, each message is sent to Jev with one question: what is the smallest model that can do this job well? When Jev is at least 60% sure, and the message is not a short reply that only makes sense inside the conversation, the job goes to the helper for that size, and the helper's reply ends with a `Done by <model>` line. If Jev is slow (over 6 seconds) or anything fails, the message goes through as if the router were not there.

| Harness | How a job reaches the smaller model |
|---|---|
| Claude Code | The main model is told to hand the job to the `jev-router:<size>` subagent. The main model still reads the message and relays the result. |
| Codex | The main model is told to `spawn_agent` on the size's model. Same relay cost as Claude Code. |
| opencode | The message itself moves onto the `jev-<size>` agent and its model, so there is no relay. |
