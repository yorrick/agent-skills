# jev-router

Sends small jobs to a smaller, cheaper model. While it is on, every message you send goes
to [Jev](https://openrouter.ai/typesafe/jev-router) on OpenRouter with one question: what
is the smallest model that can do this job well?

| Size | Jobs | Claude Code | Codex | opencode |
|---|---|---|---|---|
| tiny | a lookup, a rename, a one-line answer | Haiku 4.5 | GPT-6 Luna | Haiku 4.5 |
| everyday | a normal email, post or short document | Sonnet 5 | GPT-6 Luna | Sonnet 5 |
| large | a multi-step build, research, a full report | Opus 5.5 | GPT-6 Sol | Opus 5.5 |
| hardest | strategy, or anything where a wrong call is expensive | Fable 5.1 | GPT-6 Astra | Fable 5.1 |

Codex has three current models, so it asks Jev about three sizes. Its tiny and everyday
jobs share GPT-6 Luna. opencode reaches the Claude models through OpenRouter.

When Jev is at least 60% sure, the job goes to the helper for that size, and the reply
ends with a line such as `Done by Claude Haiku 4.5`. The session keeps the job when Jev is
less sure, when the message is a short reply that only makes sense inside the
conversation, or when the size's model is the one the session already runs on.

## Honest limits

Claude Code and Codex cannot switch the main model for one message. There, the hook
tells the main model to hand the job to a helper (a `jev-router:<size>` subagent, or
`spawn_agent` with the size's model in Codex), and the main model relays the result. The
main model still reads your message and the answer, so you save less than a real switch
would. opencode can switch, so there your message itself moves onto the helper's model.

Asking Jev takes time. It usually answers in 1 to 5 seconds, and the router waits at most
6 seconds before carrying on without it. When the router is off, the hook costs about
40 ms.

## Privacy

While the router is on, the text of every message goes to OpenRouter, to TypeSafe (the
company that makes Jev), and to whichever model Jev picks to answer the sizing question.
Keep it off for private work. The router's log keeps no message text.

## Using it

| | Claude Code | Codex | opencode |
|---|---|---|---|
| Turn on | `/jev on` | `$jev on` | `/jev on` |
| Turn off | `/jev off` | `$jev off` | `/jev off` |
| Status | `/jev status` | `$jev status` | `/jev status` |

The first time, give it the shell file that exports `OPENROUTER_API_KEY`:
`/jev on --key-file ~/path/to/openrouter.sh`. The path is remembered. The switch is shared
by all three harnesses, so turning it on in one turns it on everywhere.

`status` shows how many messages went to each size and what Jev has cost.
`classify "<message>"` sizes messages without routing them.

Codex asks you to trust the plugin's hook the first time it starts after the install
("Hooks need review"). Choose to trust it, or the router never runs there.

## Requirements and files

- `uv` on the `PATH`. The hook runs `skills/jev/scripts/jev_router.py`, which uses only
  the standard library.
- State lives in `~/.config/jev-router/`: `config.json` holds the switch, the key file
  path and `timeout_seconds` (6 by default; keep it under 7, because each harness stops
  the whole hook at 8 s), and `log.jsonl` holds one line per message.
- The size-to-model table is `skills/jev/scripts/tiers.json`. The Claude Code helpers are
  `agents/*.md`, and a test keeps them in step with the table.
