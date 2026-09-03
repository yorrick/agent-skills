# Yorrick's Claude Code Plugins

A collection of Claude Code plugins by Yorrick Jansen.

## Installation

Every plugin here works with **both Claude Code and Codex**.

**Claude Code**

```
claude plugin marketplace add yorrick/agent-skills
claude plugin install <plugin-name>@yorrick
```

**Codex**

```
codex plugin marketplace add yorrick/agent-skills
codex plugin add <plugin-name>@yorrick
```

Restart the CLI afterwards — both report that changes need one.

### Updating

Both harnesses auto-update — differently, and Codex's behaviour is undocumented.

| | Claude Code | Codex |
|---|---|---|
| Auto-update | **Per-marketplace toggle** — off by default for third-party, on for Anthropic's own | **Always on** while plugins are enabled; no dedicated opt-out |
| Fires | Once per session, at a random point within 10 min of start | At app-server startup (TUI, `codex exec`, desktop app) |
| Updates the installed plugin? | **Yes**, where enabled — version pins respected | **Yes** — reinstalls whenever the remote revision moved |
| Takes effect | After `/reload-plugins` or restart | Immediately; caches invalidated live |

Enable it on Claude Code with `/plugin` → Marketplaces → `yorrick` → Enable auto-update.

Codex's `check_for_update_on_startup` config flag does **not** disable this — it only
governs Codex's own self-update prompt (which the desktop app ignores anyway:
[codex#18543](https://github.com/openai/codex/issues/18543), closed as not planned).
The plugin auto-upgrade has no off switch short of `[features] plugins = false`.

To update by hand:

```
# Claude Code — two steps; refreshing the marketplace alone does not upgrade
claude plugin marketplace update yorrick
claude plugin update <plugin-name>@yorrick

# Codex — one step, despite the help text saying "snapshots"
codex plugin marketplace upgrade
```

Verify against the cache rather than `plugin list`, which only reflects config:

```
ls ~/.claude/plugins/cache/yorrick/<plugin-name>/
ls ~/.codex/plugins/cache/yorrick/<plugin-name>/
```

**Codex auto-update is a supply-chain path.** Anything pushed to `main` here reaches
every installed machine at next Codex startup, with no review step and no notification.
Protect this branch accordingly.

### Removing

```
claude plugin uninstall <plugin-name>@yorrick
codex plugin remove <plugin-name>@yorrick
```

Removing the *marketplace* is a bigger hammer — in Claude Code it also uninstalls every
plugin that came from it. On Codex, removing a plugin while leaving the marketplace
registered may see it reinstalled by the startup auto-upgrade; check the cache path above
after a restart.

### Renamed from `claude-code-plugins`

This repo was `yorrick/claude-code-plugins`. Existing registrations keep working
through GitHub's redirect — refresh with the update commands above. **Do not remove and
re-add the Claude marketplace**: removing its last registration also uninstalls its
plugins.

<details>
<summary>Legacy install (still works)</summary>

```
claude plugin marketplace add yorrick/claude-code-plugins
```
</details>

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

### task-status

Shows a compact, terminal-safe visual summary of the current task: what is done,
what is happening now, what comes next, what was deferred, and what is blocked.
It is strictly read-only and uses only the current conversation as evidence.

Invoke it with `/task-status:task-status` in Claude Code or `$task-status` in
Codex.

```fish
claude plugin install task-status@yorrick
codex plugin add task-status@yorrick
```

### supabase-security

Access-control rules for Supabase projects exposed directly to a browser via PostgREST:
the three enforcement layers (`GRANT`, RLS, triggers) and **the order they run in**, the
traps that cause privilege escalation, and how anon keys, JWTs and `service_role` differ.

Ships a read-only audit script that **bundles [Splinter](https://github.com/supabase/splinter)**
— Supabase's own SQL linter, the engine behind the dashboard's Security Advisor —
vendored unmodified, and adds four rules it does not have (policies with no `FOR`/`TO`,
missing `RESTRICTIVE` tenant isolation, the delete-and-reinsert column bypass, and
`TRUNCATE`, which no policy applies to). See
[`supabase-security/README.md`](supabase-security/README.md) for the credit and licence
position.

```
claude plugin install supabase-security@yorrick
```

## Local Development

For testing or development, load a plugin directly:

```bash
claude --plugin-dir ./dev-loop
claude --plugin-dir ./self-improve-skill
claude --plugin-dir ./agent-session-monitor
claude --plugin-dir ./task-status
```
