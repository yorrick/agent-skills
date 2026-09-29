---
name: jev
description: Switch the Jev model router on or off, set its Claude Code mode, run its fork check, or show its status. Use when the user types /jev, $jev, "jev on", "jev off", "jev status", "jev mode", asks to turn the Jev router on or off, asks how many messages went to each size, asks what Jev has cost, or asks about the fork check (snapshots, replays, the report). The router asks Jev (TypeSafe's model on OpenRouter) about each message. In Codex and opencode it sends small jobs to a smaller, cheaper model. In Claude Code it predicts how long the job is and prices a fresh subagent against doing it in the session; in shadow mode, the default, it only logs that decision, and only live mode tells the session to delegate. It is off until the user turns it on.
---

# Jev router switch

Run the router script in this skill's `scripts/` directory with the user's word: `on`, `off` or `status` (no word means `status`).

```bash
uv run --quiet --script <this skill's directory>/scripts/jev_router.py status
```

Show the user the script's output as it is. It already carries the privacy reminder when the router is on. Do nothing else.

- `on` needs, once, the shell file that exports `OPENROUTER_API_KEY`: add `--key-file <path>`. The path is remembered for later `on`s. If `on` fails because no key file is known, ask the user for the path. Never search for keys and never print one.
- `classify "<message>" ...` sizes messages without routing anything and prints a table of what Jev picked and how sure it was. Add `--harness codex` or `--harness opencode` to use those harnesses' sizes.
- `JEV_ROUTER=off` in a run's environment turns the router off for that run, whatever the switch says; `JEV_ROUTER=on` opts a headless run (`claude -p`, `codex exec`, `opencode run`) in, such as an eval.
- `mode shadow|capture|live` (runs `jev_router.py mode ...`) sets the Claude Code cache-aware router's mode: `shadow` decides and logs but tells the session nothing, `capture` also saves a snapshot of each job it would delegate, and `live` tells the session to hand the job to a fresh subagent. Add `--dir <path>` with `capture` to change where snapshots are saved.

## Fork check

The fork check replays the jobs the router selected in `capture` mode, both kept and delegated, to measure real cost, time and quality before turning on `live` mode. Run its commands from `skills/jev/scripts/fork_check.py`, in this order: `check` (restore-check new snapshots), `list` (snapshots and what each real turn did outside the machine), `mark` (mark a snapshot safe or skip to replay, or inconclusive to take a stuck job out of the report), `replay` (restore a marked snapshot into two clones and run keep and delegate; it also resumes a job an interrupted replay left behind), `judge` (a blind Codex verdict on the two results), `publish` (push both results to a private copy and open the comparison PR), `report` (the fork check's pass or fail over its 20 jobs), and `shadow` (shadow-mode decisions against what really happened).

Replay a job captured in a worktree before removing that worktree: once its checkout is gone, the job can only be inconclusive.

## What the router does

One switch, in `~/.config/jev-router/`, covers Claude Code, Codex and opencode. While it is on, each message the user types (up to its first 4,000 characters) is sent to Jev; in Claude Code, so are the last 1,500 characters of the assistant's previous reply. Each size maps to a model and a thinking level inside the same harness (the table is `scripts/tiers.json`). In Codex and opencode, Jev answers two typed questions: what is the smallest model that can do this job well, and does the message only make sense inside the conversation? When Jev gives its pick at least a 60% probability, and the message is not a short reply that only makes sense inside the conversation, the job goes to the helper for that size, and the reply ends with a line such as `Done by GPT-6 Luna at max thinking`. Headless runs (`claude -p`, `codex exec`, `opencode run`) are routed only with `JEV_ROUTER=on`. If Jev is slow (over 2 seconds) or anything fails, the message goes through as if the router were not there.

| Harness | How a job reaches its model and thinking level |
|---|---|
| Claude Code | Jev predicts how many model calls the job will take, and the router prices doing it in the session against briefing a fresh `jev-router:<size>` subagent. Only in `live` mode, and only when delegating is expected to save at least $0.25 and 15% with at most a 20% chance of costing more, is the main model told to brief that subagent and review its result. `shadow` (the default) and `capture` tell the session nothing. |
| Codex | The main model is told to `spawn_agent` with the size's model and `reasoning_effort`, unless it is certain it already runs that model at that level. It still reads the message and relays the result. |
| opencode | The message itself moves onto the `jev-<size>` agent, model and thinking level, so there is no relay. |
