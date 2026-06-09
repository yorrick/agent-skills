# Yorrick's Claude Code Plugins

A collection of Claude Code plugins by Yorrick Jansen.

## Installation

Add this repository as a marketplace:

```
claude plugin marketplace add yorrick/claude-code-plugins
```

Then install any plugin:

```
claude plugin install <plugin-name>@yorrick
```

## Plugins

### dev-loop

Automated development loop that composes existing Claude Code commands into a full lifecycle: brainstorm, plan, implement, create PR, then iteratively review (simplify + code review + security review) until clean.

**Commands:**
- `/dev-loop` — Full lifecycle: interactive brainstorming and planning, then automated implementation and review loop
- `/review-loop` — Review loop only on an existing PR (skip brainstorm + implementation)

**Prerequisites:**
- [superpowers](https://github.com/obra/superpowers) plugin (brainstorming, writing-plans, executing-plans skills)
- [code-review](https://github.com/anthropics/claude-code-plugins) plugin (`/code-review:code-review`)
- `/simplify` and `/security-review` (built-in)
- `gh` CLI (for creating PRs)

```
claude plugin install dev-loop@yorrick
```

### self-improve-skill

Automatically reflects on skills used during sessions and proposes improvements.

```
claude plugin install self-improve-skill@yorrick
```

### pr-review

Multi-provider interactive PR code review using Claude Code, Gemini CLI, and Codex CLI.

```
claude plugin install pr-review@yorrick
```

### agent-session-monitor

Meta-agent skill for monitoring and steering Claude Code, Codex, and other terminal coding agents through tmux panes. The skill content is portable Agent Skills format; local Claude/Codex discovery should symlink to `agent-session-monitor/skills/agent-session-monitor`.

```
claude plugin install agent-session-monitor@yorrick
```

## Local Development

For testing or development, load a plugin directly:

```bash
claude --plugin-dir ./dev-loop
claude --plugin-dir ./self-improve-skill
claude --plugin-dir ./agent-session-monitor
```
