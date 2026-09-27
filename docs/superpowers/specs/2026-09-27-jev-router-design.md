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
| Each size mapped to a model and thinking level the harness actually has, smallest to biggest | `tiers.json` `harnesses` |
| One helper per size on its model and level, ending with a line naming both | `agents/*.md` (Claude Code), spawn message (Codex), `jev-<size>` agents (opencode) |
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

| Harness | tiny / everyday / large / hardest | Mechanism |
|---|---|---|
| Claude Code | Haiku 4.5 / Opus 5.5 low / Opus 5.5 high / Opus 5.5 max | hook context: hand the job to the `jev-router:<size>` subagent, which pins `model` and `effort` |
| Codex | Luna medium / Luna max / Sol high / Astra max | hook context: `spawn_agent` with `model` and `reasoning_effort` (plugins cannot ship agents) |
| opencode | GLM 5.3 flash high / high / max / max | `chat.message` moves the message onto the `jev-<size>` agent, model and `model.variant` |

### Revision: a thinking level per size (2026-09-27)

The first version mapped each size to a model only. The user asked for a thinking
level as well, for opencode to use only GLM 5.3 flash and DeepSeek v4.1 flash, and
for GLM never to run at `low`. The tiers were then checked against Artificial
Analysis (Intelligence Index and cost per task, per effort level):

- Claude: Opus 5.5 scores higher than Sonnet 5 and Fable 5.1 at every price (for
  example Opus low 42 at $0.55 against Sonnet medium 28 at $1.00, and Opus max 58
  at $5.98 against Fable max 53 at $7.63), so both dropped out.
- Codex: Luna, Sol and Astra hold; everyday moved to Luna max (37 at $0.07).
- opencode: GLM 5.3 flash scores 42 at $0.25 against DeepSeek v4.1 flash's 39 at
  $0.27. On coding the independent numbers split (Terminal-Bench 4.0 33% vs 27%
  for GLM, AutomationBench 60% vs 69% for DeepSeek, SciCode tied); DeepSeek's
  stronger coding figures are self-reported. By the rule "drop a setting another
  one beats on score and cost", every opencode size runs GLM. Artificial Analysis
  does not split GLM by effort, so `high` versus `max` is a judgment call.

Subscriptions are billed in usage limits; we assume those limits are spent in
proportion to API prices.

The session keeps a job only when it is certain it already runs the tier's model
at the tier's thinking level. The user rejected keeping large jobs by model alone,
because nothing shows a hand-off costs more than running at the wrong level.
Neither the Claude Code nor the Codex hook can see the session's thinking level
(verified: no stdin field or environment variable carries it, and Claude's
`CLAUDE_EFFORT` is inherited from the parent, not the session), but Claude states
its level correctly, including after a mid-session `/effort`. A model that is not
certain hands off.

### Revision: interactive sessions only, inside one harness

The user's review rules have Codex review Claude's work and Claude review Codex's,
through headless CLIs that pin the reviewer's model and effort. The router must not
touch those runs, so it routes only when a person is typing, failing closed:

- Claude Code: `CLAUDE_CODE_SESSION_ATTENDED` is `1` in the TUI and `0` under
  `claude -p`, set by Claude Code itself.
- Codex: the transcript's first line records `source`, which is `exec` for
  `codex exec` and `cli` or `vscode` for a person (checked on 35 real sessions).
  `codex exec resume` appends to an interactive transcript without changing that
  line, so the hook also requires `permission_mode` to differ from
  `bypassPermissions`, which is what every `codex exec` reports. A person who
  bypasses approvals is therefore not routed; that loses routing, never a review.
- opencode: the TUI runs its server in `src/cli/tui/worker.js`, while `opencode run`
  runs `src/index.js`. `opencode run --attach` sends its message to a server a TUI
  started, so the message must also name its agent: the TUI always does, and
  `opencode run` only with an explicit `--agent`. A TUI attached to `opencode serve`
  is not routed, which again fails closed. Known gap: `opencode run --attach` to a
  live TUI with an explicit `--agent` is routed. It runs in the TUI's process through
  the same internal event path, and its hook input differs only in what the caller
  chose to pass (verified: argv and stack traces are identical), so no plugin can
  tell it from typing. The user's review commands never attach.

Headless text is never sent to Jev either. Every tier names a model of its own
harness, and a test enforces it, so a helper's work is still that harness's work.

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
  `urlopen`'s timeout only bounds each socket read. `timeout_seconds` in
  `config.json` can only lower it, because the harnesses stop the whole hook at
  8 s and startup plus logging need the rest. A timed-out call may still be billed; its
  cost is never reported back, and `status` says so.
- **Never in the way.** Off, a timeout, an HTTP error, unparseable JSON, an
  unknown size, a missing key, or bad stdin all print nothing and exit 0. The
  opencode module catches everything, including a broken `tiers.json` at load,
  because opencode drops a message whose hook throws. Each harness also stops
  the whole hook at 8 s, which covers `uv` startup and logging as well as Jev.
- **Strict verdicts.** A verdict counts only with a known size, a numeric
  confidence from 0 to 100 (59.6 is not rounded up to 60), and an explicit
  boolean `follow_up`. Anything else is an error, and the router carries on.
  OpenRouter's billed cost is logged even when the answer is unusable.
- **Codex spawns with `fork_turns "none"`.** A full-history fork inherits the
  parent's model and ignores the override, so the hand-off asks for a fresh
  agent and puts the needed context in its message.
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
  cost, latency and harness. Error entries use fixed labels such as
  `HTTP 500` or `BadAnswer: unknown size`, never Jev's reply, which could echo
  the message.
- **"Routed" means offered.** In Claude Code and Codex the hook can only
  advise, and the session keeps a job only when it is certain it already runs
  the helper's model at the helper's thinking level.
  `status` says so rather than claiming the helper did the work.
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
