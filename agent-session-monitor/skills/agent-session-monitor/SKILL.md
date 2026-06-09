---
name: agent-session-monitor
description: Monitor and steer Claude Code, Codex, or other terminal coding agents running in tmux panes. Use when asked to supervise, babysit, drive, or automate agent sessions through tmux, including reading agent output, detecting idle/blocked/context-boundary states, sending approved commands, clearing or compacting context at explicit boundaries, and coordinating multi-agent workflows without duplicating Claude/Codex-specific skill content.
---

# Agent Session Monitor

Use this skill to act as a meta agent for terminal agents running inside tmux.
It is intentionally platform-neutral: the skill body uses the open Agent Skills
format, and product-specific behavior lives in small references.

## Compatibility Model

Keep one versioned source of truth:

```text
agent-session-monitor/
  .claude-plugin/plugin.json
  skills/agent-session-monitor/
    SKILL.md
    scripts/
    references/
```

Expose `skills/agent-session-monitor` to host-specific discovery paths with
symlinks or plugin installation. Do not copy the skill body into separate Claude
and Codex versions.

Read `references/platforms.md` when you need install-path or command details.
Read `references/gsd.md` only when monitoring GSD workflow panes.

## Operating Rules

- Treat tmux panes as the source of truth. Capture the pane before every
  decision and again after sending input.
- Never interrupt a pane that is still working, compacting, streaming tokens, or
  showing an interrupt hint.
- Follow the watched agent's own next-step recommendation before applying a
  generic workflow. If the agent says to clear context, ask the user, or run a
  specific command, treat that as higher-signal than a rigid state machine.
- Send only commands that the user explicitly approved or that the watched agent
  explicitly requested and are low-risk in the current workflow.
- If a command is rejected, do not try an alternate command automatically. Show
  the rejection, explain the proposed correction, and get explicit approval
  unless the correction was already approved for this workflow.
- If a design appears to need fallback behavior, stop and ask first. This
  includes retry loops, alternate slash commands, heartbeats, replacement
  monitors, and rollover behavior after a monitor process exits.
- Do not clear or compact context when the agent is asking a real question,
  reporting ambiguity, requesting a decision, or waiting on credentials,
  approvals, destructive operations, or external state.

## Resolve Targets

List panes:

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} | #{pane_title} | #{pane_current_command} | #{pane_current_path}'
```

Use real tmux targets such as `session:0.0`. Version strings like `2.1.170`
in a pane title or command are agent versions, not tmux targets.

If the user gave fuzzy targets, resolve them from pane title, current command,
and working directory. Ask only when two or more panes match equally well.

## Baseline

From this skill directory:

```bash
scripts/watch-tmux-agents.sh --once --include-idle <target> [<target>...]
```

For each target, also capture recent output directly when making a decision:

```bash
tmux capture-pane -t <target> -p -S -80
```

Classify the pane:

- `WORKING`: busy, compacting, streaming, or not safely interruptible.
- `IDLE`: at a prompt with no actionable boundary.
- `ASKING_IDLE`: waiting for a decision or guidance.
- `CONTEXT_REQUESTED_IDLE`: agent says to clear, compact, resume, or start a
  fresh context.
- `PLAN_DONE_IDLE` / `REVIEW_DONE_IDLE`: workflow-specific boundary.
- `ERROR`: crash, rejected command, rate limit, unknown command, or visible
  failure.
- `GONE`: tmux target disappeared.

## Monitor Loop

Choose the monitor execution surface explicitly for the current host and user
request. Common choices are:

- Claude Code `Monitor` tool, when the user wants a Claude-managed persistent
  task.
- A named tmux session, when the user wants inspectable local logs.

For an inspectable tmux monitor:

```bash
tmux new-session -d -s agent-session-monitor 'cd <skill-dir> && scripts/watch-tmux-agents.sh <target> [<target>...]'
```

When the watcher emits an event:

1. Capture the pane with `tmux capture-pane`.
2. Read the watched agent's latest recommendation.
3. Decide whether the action is user-approved, agent-requested, or blocked.
4. Send input only when action is clearly allowed.
5. Capture the pane again to verify acceptance.

## Sending Input

Use the helper so text and Enter are sent separately:

```bash
scripts/send-to-tmux-agent.sh <target> '<text to send>'
```

For manual sends:

```bash
tmux send-keys -t <target> '<text to send>'
sleep 1
tmux send-keys -t <target> Enter
```

If the pane rejects the command, stop. Do not retry with a guessed alternative.

## Context Commands

Claude Code and Codex both use `/clear` for starting a fresh conversation in the
same terminal session. Codex also has `/compact` to summarize the visible
conversation and free tokens.

Use context commands only when:

- the user asked for that automation in this workflow,
- the watched agent explicitly recommends it, or
- the current skill invocation explicitly says context clearing/compaction is
  allowed at that boundary.

If the watched agent says "ask what the next step is" or asks a question, do not
clear first. Surface the question to the user.

## Output

Keep the user informed with concise status:

- targets being monitored,
- current state per target,
- actions sent,
- errors or blocked decisions,
- how to attach to any monitor tmux session.

Never hide uncertainty. A meta agent that sends the wrong command into a live
agent pane can mutate work the user did not intend to touch.
