# jev-router

Sends each job where it costs least. While it is on, every message you type goes to
[Jev](https://openrouter.ai/typesafe/jev-1.13), TypeSafe's decision model, through
OpenRouter's Decisions API.

- **Codex and opencode (0.2.0):** Jev answers two typed questions, what is the smallest
  model that can do this job well, and does the message only make sense inside the
  conversation? It picks one of the sizes below and gives the probability of each, and the
  job goes to the model and thinking level that fit it, which usually means a cheaper one.
- **Claude Code (0.3):** Jev predicts how many model calls the job will take, and the router
  prices doing it in the session against briefing a fresh subagent on the size's model. It
  starts in shadow mode: it decides and logs, and tells the session nothing (see
  [below](#claude-code-cache-aware-delegation-03)).

| Size | Jobs | Claude Code | Codex | opencode |
|---|---|---|---|---|
| tiny | a lookup, a rename, a one-line answer or a one-line code change | Haiku 4.5 | GPT-6 Luna, medium | GLM 5.3 flash, high |
| everyday | a normal email, post or short document, or a small, well-defined code change or bug fix | Opus 5.5, low | GPT-6 Luna, max | GLM 5.3 flash, high |
| large | a multi-step build, research, a full report, or a feature that spans several files | Opus 5.5, high | GPT-6 Sol, high | GLM 5.3 flash, max |
| hardest | strategy, architecture or data design, or anything where a wrong call is expensive | Opus 5.5, max | GPT-6 Astra, max | GLM 5.3 flash, max |

Jev reads these descriptions literally, as the options of a typed Choice question, so each
names coding work as well as writing: without that, coding tasks came back split between
two sizes at about 50%.

Each cell is a model and its thinking level (Haiku 4.5 has none). They were chosen from
[Artificial Analysis](https://artificialanalysis.ai/?cost=intelligence-vs-cost-per-task)
benchmarks (checked 2026-09-27): its Intelligence Index for how capable a setting is, and
its cost per task for what it costs. Within a harness, a setting that another one beats on
both score and cost is left out, which is why Sonnet 5 and Fable 5.1 are absent: Opus 5.5
scores higher at every price. Those costs are API prices on Artificial Analysis's own
benchmark tasks. On a Claude Max or ChatGPT subscription you pay in usage limits instead,
and we assume those limits are spent in proportion to API prices, since that is the best
proxy available.

In Codex and opencode, when Jev gives its pick at least a 60% probability, the job goes to
the helper for that size, and the reply ends with a line such as `Done by GPT-6 Luna at max
thinking`. The session keeps the job when Jev is less sure, when the message is a short
reply that only makes sense inside the conversation, or when the session is certain it
already runs that model at that thinking level. In Claude Code, the size only names the
`jev-router:<size>` subagent a delegated job goes to.

## What it never does

- **Leave the harness.** Claude Code only routes to Claude models, Codex to GPT models, and
  opencode to GLM and DeepSeek. So your cross-AI review rules still hold: work done by a
  helper is still that harness's work. A test fails if a tier names another harness's model.
- **Touch headless runs, unless you ask.** Only sessions where you are typing are routed.
  `claude -p`, `codex exec` and `opencode run` are reviews and automation that pin their own
  model and thinking level, so the router neither changes them nor sends their text to Jev,
  unless you opt one in with `JEV_ROUTER=on` (an eval, say). `JEV_ROUTER=off` turns the
  router off for any run, whatever the switch says. When
  it cannot tell, it treats the session as headless: a Codex session you start with
  approvals bypassed looks like `codex exec`, so it is not routed. One gap remains:
  `opencode run --attach` to your running TUI with an explicit `--agent` runs inside the
  TUI and looks exactly like typing there, so it is routed. Nothing in opencode's hook
  tells the two apart (verified), and your review commands do not attach.
- **Block a message.** If Jev is slow (over 2 seconds) or anything fails, the message goes
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

Asking Jev takes a moment: it answered in 0.16 to 0.5 seconds in our probes, and the router
waits at most 2 seconds before carrying on without it. When the router is off, the hook costs about
40 ms.

## Claude Code: cache-aware delegation (0.3)

In Claude Code the router also decides, once per message, whether a fresh subagent would
finish the job for less. Re-reading the whole conversation on every call is most of a long
job's cost, and a subagent starts from a brief instead of the full context. It reads the
session's current context size, its recent per-call cost and Jev's prediction of how many
calls the job will take, then delegates only when the expected saving is large enough and
the chance of losing money is low.

It has three modes, set with `/jev mode shadow|capture|live`: `shadow` (the default)
decides and logs but tells the session nothing; `capture` does the same and also saves a
snapshot of each job it would delegate; `live` tells the session to hand the job to a
fresh subagent.

Before `live` mode is turned on, the fork check replays the jobs `capture` selected, both
kept and delegated, to measure real cost, time and quality on the user's own work. Run its
commands, in order: `check`, `list`, `mark`, `replay`, `judge`, `publish`, `report`, and
`shadow` (shadow-mode decisions against what really happened) at any time. Replay a job
captured in a worktree before removing that worktree: once its checkout is gone, the job is
inconclusive. The judge (Codex) reads both results, but its copies leave out every ignored
file outside dependency folders and virtualenvs, any other copy of a restored secret such as
`.env`, and anything in git beyond HEAD's history and the replay's starting point: each
copy's `.git` is fetched fresh from the replay's clone, never copied, and each virtualenv is
pointed at its copy. A result holding a link out of the repository is not judged. See
[the design spec](../docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md)
for the full design and the validation it must pass.

## Privacy

While the router is on, every message you type goes to OpenRouter and to TypeSafe (the
company that makes Jev), up to its first 4,000 characters. In Claude Code 0.3, each message
also sends the last 1,500 characters of the assistant's previous reply. This holds in
shadow and capture modes too, since the router decides every message even when it tells the
session nothing. If you turned the router on under 0.2.0, this wider sending starts as soon
as the plugin updates to 0.3. Headless runs are routed, and so sent, only with
`JEV_ROUTER=on`. Keep the router off for private work. The router's log keeps no message
text.

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
  path and `timeout_seconds` (2, and it can only be lowered, because each harness stops
  the whole hook at 8 s), and `log.jsonl` holds one line per message.
- The size table is `skills/jev/scripts/tiers.json`. The Claude Code helpers are
  `agents/*.md`, and a test keeps them in step with the table.
