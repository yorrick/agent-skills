# Installing agent-skills for opencode

## Prerequisites

- [opencode](https://opencode.ai) installed

## Installation

Add the plugin to the `plugin` array in your `opencode.json` (global or project-level):

```json
{
  "plugin": ["yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git"]
}
```

Restart opencode. The plugin installs through opencode's plugin manager and registers
every plugin's skills and commands.

Verify by asking "list your skills" (you should see `agent-session-monitor`, `flowchart`,
`pr-review`, `reflect`, `supabase-security`, `task-status`, and `visual-design-review`)
or by typing `/dev-loop`.

opencode uses its own plugin install. If you also use Claude Code or Codex, install this
repository separately for each harness.

## What gets registered

- Skills from every plugin that has a `skills/` directory.
- Commands from every plugin that has a `commands/` directory (`dev-loop`,
  `review-loop`, and `workflow`).
- Not registered: `self-improve-skill`'s session hooks, which have no opencode
  equivalent yet.
- Not translated: Claude-style `allowed-tools` and `argument-hint` frontmatter.
  opencode has no per-command tool restrictions, so a command that is tool-limited in
  Claude Code is not limited here.

A skill or command you defined yourself wins over the plugin's version.

## Updating

opencode installs through a git-backed package spec. Some opencode and Bun versions pin
the resolved git dependency in a lockfile or cache, so a restart may not pick up the
newest commit. If updates do not appear, remove the cached package and restart:

```bash
rm -rf ~/.cache/opencode/packages/yorrick-agent-skills*
```

The glob removes branch-pinned installs too (specs using `#branch`), which is
intended: they are all replaced on the next start.

No git tags exist yet, so installs track `main`.

## Troubleshooting

### Plugin not loading

1. Check the logs: `opencode run --print-logs "hello" 2>&1 | grep -i agent-skills`
2. Verify the plugin line in your `opencode.json`
3. Make sure you are running a recent version of opencode

### Skills not found

Use opencode's `skill` tool to list what was discovered.
