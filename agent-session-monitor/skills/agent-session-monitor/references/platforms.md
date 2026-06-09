# Platform Notes

## Shared Agent Skills Format

Use one `SKILL.md` with YAML frontmatter containing at least `name` and
`description`. Put deterministic helpers in `scripts/` and detailed platform
notes in `references/`.

Keep host-specific metadata outside the portable skill body where possible.
Codex-specific UI metadata can live in `agents/openai.yaml`. Claude-specific
plugin metadata lives in `.claude-plugin/plugin.json`.

## Install Paths

Versioned source in this repository:

```text
agent-session-monitor/skills/agent-session-monitor
```

Codex documented user-scope discovery:

```text
~/.agents/skills
```

Claude Code user-scope discovery commonly uses:

```text
~/.claude/skills
```

For older or locally customized Codex setups that still scan:

```text
~/.codex/skills
```

Prefer symlinks from host-specific paths to the versioned skill directory:

```bash
ln -s <repo>/agent-session-monitor/skills/agent-session-monitor ~/.agents/skills/agent-session-monitor
ln -s <repo>/agent-session-monitor/skills/agent-session-monitor ~/.claude/skills/agent-session-monitor
ln -s <repo>/agent-session-monitor/skills/agent-session-monitor ~/.codex/skills/agent-session-monitor
```

Restart the host agent after changing discovery paths.

## Commands

Claude Code:

- `/clear` starts a fresh context in the active terminal session.
- Some workflows may print slash-command suggestions in a different namespace
  than the currently installed command names. Do not auto-correct unless the
  mapping is already approved for the workflow.

Codex:

- `/clear` clears the terminal and starts a fresh chat in the same CLI session.
- `/compact` summarizes the visible conversation to free context tokens.
- `/status` shows context usage and rate-limit/session details.

## Host Tool Mapping

The skill itself only assumes shell access and tmux.

Choose the long-running monitor surface explicitly for the active host:

- Codex: a named tmux session is inspectable by the user.
- Claude Code: the Monitor tool is useful when the user wants a
  Claude-managed persistent task.

For notifications, use only a notification mechanism available in the current
host and approved by the user or current workflow.
