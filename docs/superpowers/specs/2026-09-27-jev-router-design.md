# jev-router design

## Goal

The user asked for a router that asks Jev (TypeSafe's `typesafe/jev-router` on
OpenRouter) one question about every message they send: what is the smallest
model that can do this job well? Small jobs then go to a smaller, cheaper model
instead of the biggest one. It must work in Claude Code, Codex and opencode.

Their requirements, and where each one is met:

| Requirement | Where |
|---|---|
| Four sizes: tiny, everyday, large, hardest | `tiers.json` `jobs` |
| One size per model actually available, smallest to biggest; fewer sizes if fewer models | `tiers.json` `harnesses` |
| One helper per size on its model, ending with a line naming the model | `agents/*.md` (Claude Code), spawn message (Codex), `jev-<size>` agents (opencode) |
| Hand off when Jev is at least 60% sure; keep follow-up replies in the session | `decide()` |
| Never slow down or block a message; carry on if Jev is slow or anything fails | `route()`, `cmd_hook()`, `hooks/opencode.js` |
| On/off switch, off by default, status with counts per size and Jev's cost | `on`, `off`, `status` |
| Say so if per-message model switching is impossible, and build the closest thing | below |

## Per-message switching, per harness

Only opencode can change the model of a single message: its `chat.message` hook
may rewrite the message's agent and model (verified empirically). Claude Code and
Codex cannot. Their `UserPromptSubmit` hooks can only add context for the turn,
so the closest thing is to tell the main model to delegate. The main model still
reads every message and relays the helper's answer. The saving is therefore the
difference between the helper doing the work and the main model doing it, minus
that relay.

| Harness | Sizes | Mechanism |
|---|---|---|
| Claude Code | Haiku 4.5, Sonnet 5, Opus 5.5, Fable 5.1 | hook context: hand the job to the `jev-router:<size>` subagent |
| Codex | GPT-6 Luna (tiny + everyday), Sol, Astra | hook context: `spawn_agent` with the size's `model` (plugins cannot ship agents; `spawn_agent` takes a model) |
| opencode | Claude Haiku 4.5 to Fable 5.1 via OpenRouter (its one logged-in provider) | `chat.message` moves the message onto the `jev-<size>` agent and model |

Codex has three current models, so two sizes share one. Tiny and everyday merge
because both are single-shot writing, while the step from everyday to large (a
multi-step build) is where capability starts to matter.

When the chosen size runs on the model the session already uses (Opus 5.5 in
Claude Code, GPT-6 Sol in Codex), the main model is told to keep the job, because
delegating would add a hop and save nothing.

## Components

- `skills/jev/scripts/jev_router.py`: the only logic. It is stdlib-only, so the
  hook costs about 40 ms when the router is off. Subcommands: `hook <harness>`,
  `on [--key-file]`, `off`, `status`, `classify`.
- `skills/jev/scripts/tiers.json`: the size-to-model table, read by the script
  and by `hooks/opencode.js`.
- `hooks/hooks.json` (Claude Code) and `hooks/codex.json` (Codex): the same
  `UserPromptSubmit` command with the harness named explicitly.
- `hooks/opencode.js`: loaded by the repository's opencode entry. It registers
  the helper agents and the `/jev` command, then applies the script's decision.
- `skills/jev/SKILL.md`: the switch, invoked as `/jev` or `$jev`.

## Decisions

- **Deadline.** Across 26 measured calls, Jev answered in 0.8 to 8.4 s, with a
  median of about 3.5 s. It routes its own sizing question to whichever model it
  picks. The cap is 6 s of wall-clock time, enforced with a thread because
  `urlopen`'s timeout only bounds each socket read. It can be changed with
  `timeout_seconds` in `config.json`. A timed-out call may still be billed; its
  cost is never reported back, and `status` says so.
- **Never in the way.** Off, a timeout, an HTTP error, unparseable JSON, an
  unknown size, a missing key, or bad stdin all print nothing and exit 0. The
  opencode module catches everything, because opencode drops a message whose
  hook throws.
- **What is not sent.** Prompts starting with `/` or `$` are skipped, because
  they are commands or skill calls such as `/jev off`. So are messages from any
  opencode agent other than `build`, because moving a plan-mode message onto a
  helper would lift plan mode's limits. Jev sees at most the first 4000
  characters.
- **One switch, one key source.** State lives in `~/.config/jev-router/`
  (`JEV_ROUTER_HOME` overrides it). The key is read only from the shell file
  given to `on --key-file`. The harness processes do not export
  `OPENROUTER_API_KEY`, and a second source would be an unapproved fallback.
- **A kept job gets a note.** When Jev answers but the job stays in the session
  (a follow-up, or under 60% sure), the model is told to handle it itself and to
  add no "Done by" line. In the opencode end-to-end run, the session's DeepSeek
  model otherwise copied the previous turn's "Done by Claude Sonnet 5" line and
  misattributed its own answer. A timeout or an error still adds nothing.
- **opencode helpers are hidden subagents.** `chat.message` can move a message
  onto a `mode: "subagent", hidden: true` agent (verified), so the helpers stay
  out of the Tab list of primary agents.
- **The log keeps no message text.** It records the outcome, size, confidence,
  cost, latency and harness.
- **Privacy.** While the router is on, message text reaches OpenRouter, TypeSafe,
  and whichever model Jev picks to answer the sizing question. Probes saw that
  question answered by `openai/gpt-6-luna`, `deepseek/deepseek-v4.1-flash`,
  `google/gemini-3.8-flash` and `openai/gpt-6-sol`. `on` and `status` say this.

## Repository changes

- `sync_manifests.py` gave Codex `"hooks": "./hooks/"`. Codex reads that field
  as a file and fails with "Is a directory", so no plugin hook ever ran in Codex.
  It now emits `./hooks/codex.json` when a plugin ships that file. The file is
  separate because Claude hook files may use events Codex lacks or shell out to
  `claude`. self-improve-skill ships none, so its effective Codex behaviour (no
  hooks) is unchanged.
- The opencode entry loads each plugin's `hooks/opencode.js` and merges hooks by
  name.

## Testing

- `tests/test_jev_router.py` runs the script as a subprocess against a local
  OpenRouter stand-in. It covers every failure path, the 60% boundary,
  follow-ups, each harness's output, status counts, and the helpers matching
  `tiers.json`. Through a node harness it also covers the opencode module
  inside the real entry.
- Acceptance: ten real messages through `classify`, eight from tiny to hardest
  plus two follow-ups. Nine were sized as intended and one hit the 6 s cap.
- End-to-end runs in each harness with isolated state.
