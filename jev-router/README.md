# jev-router

Sends each job to the model and thinking level that fit it, which usually means a cheaper
one. While it is on, every message you type goes to
[Jev](https://openrouter.ai/typesafe/jev-router) on OpenRouter with one question: what is
the smallest model that can do this job well?

| Size | Jobs | Claude Code | Codex | opencode |
|---|---|---|---|---|
| tiny | a lookup, a rename, a one-line answer | Haiku 4.5 | GPT-6 Luna, medium | GLM 5.3 flash, high |
| everyday | a normal email, post or short document | Opus 5.5, low | GPT-6 Luna, max | GLM 5.3 flash, high |
| large | a multi-step build, research, a full report | Opus 5.5, high | GPT-6 Sol, high | GLM 5.3 flash, max |
| hardest | strategy, or anything where a wrong call is expensive | Opus 5.5, max | GPT-6 Astra, max | GLM 5.3 flash, max |

Each cell is a model and its thinking level (Haiku 4.5 has none). They were chosen from
[Artificial Analysis](https://artificialanalysis.ai/?cost=intelligence-vs-cost-per-task)
benchmarks (checked 2026-09-27): its Intelligence Index for how capable a setting is, and
its cost per task for what it costs. Within a harness, a setting that another one beats on
both score and cost is left out, which is why Sonnet 5 and Fable 5.1 are absent: Opus 5.5
scores higher at every price. Those costs are API prices on Artificial Analysis's own
benchmark tasks. On a Claude Max or ChatGPT subscription you pay in usage limits instead,
and we assume those limits are spent in proportion to API prices, since that is the best
proxy available.

When Jev is at least 60% sure, the job goes to the helper for that size, and the reply
ends with a line such as `Done by Claude Opus 5.5 at low thinking`. The session keeps the
job when Jev is less sure, when the message is a short reply that only makes sense inside
the conversation, or when the session is certain it already runs that model at that
thinking level.

## What it never does

- **Leave the harness.** Claude Code only routes to Claude models, Codex to GPT models, and
  opencode to GLM and DeepSeek. So your cross-AI review rules still hold: work done by a
  helper is still that harness's work. A test fails if a tier names another harness's model.
- **Touch headless runs.** Only sessions where you are typing are routed. `claude -p`,
  `codex exec` and `opencode run` are reviews and automation that pin their own model and
  thinking level, so the router neither changes them nor sends their text to Jev.
- **Block a message.** If Jev is slow (over 6 seconds) or anything fails, the message goes
  through as if the router were not there.

## Honest limits

Claude Code and Codex cannot switch the main model for one message. There, the hook tells
the main model to hand the job to a helper (a `jev-router:<size>` subagent, or
`spawn_agent` with the size's model and reasoning effort in Codex), and the main model
relays the result. The main model still reads your message and the answer, so you save
less than a real switch would. opencode can switch, so there your message itself moves
onto the helper's model and thinking level.

Neither hook can see the session's current thinking level, so the main model decides
whether it already matches. Claude knows its own level. A model that is not certain hands
the job off.

Asking Jev takes time. It usually answers in 1 to 5 seconds, and the router waits at most
6 seconds before carrying on without it. When the router is off, the hook costs about
40 ms.

## Privacy

While the router is on, the text of every message you type goes to OpenRouter, to TypeSafe
(the company that makes Jev), and to whichever model Jev picks to answer the sizing
question. Keep it off for private work. The router's log keeps no message text.

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
  path and `timeout_seconds` (6, and it can only be lowered, because each harness stops
  the whole hook at 8 s), and `log.jsonl` holds one line per message.
- The size table is `skills/jev/scripts/tiers.json`. The Claude Code helpers are
  `agents/*.md`, and a test keeps them in step with the table.
