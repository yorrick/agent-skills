# Agent Instructions

Agent skills and plugins for **both Claude Code and Codex**.

`CLAUDE.md` is a symlink to this file, so there is one set of instructions and it cannot
drift between harnesses.

## The rule that matters

`skills/<name>/SKILL.md` and `references/` are shared **verbatim** between harnesses —
Codex adopted the same format. The JSON manifests are **generated** from each plugin's
`plugin.toml`; never hand-edit a `plugin.json` or `marketplace.json`. After changing any
`plugin.toml`:

```bash
uv run scripts/sync_manifests.py
```

CI fails if the generated files drift. This is not theoretical: before the generator
existed, the marketplace advertised `dev-loop@0.14.0` while the plugin was `0.29.0`.

The two manifests are **not** identical. Claude Code discovers `skills/` and `commands/`
by convention; the Codex manifest declares them explicitly (`"skills": "./skills/"`),
matching what OpenAI's own manifests do rather than relying on fallback behaviour.

## Plugin layout

```
<plugin>/
├── plugin.toml                   ← the ONLY file you hand-edit
├── skills/<name>/
│   ├── SKILL.md                  ← shared
│   └── references/               ← shared
├── commands/                     ← optional, shared
├── hooks/                        ← optional, shared
├── .claude-plugin/plugin.json    ← generated
└── .codex-plugin/plugin.json     ← generated
```

Marketplace manifests: `.claude-plugin/marketplace.json` (Claude Code — Codex reads it as
a legacy path) and `.agents/plugins/marketplace.json` (Codex canonical). Both generated.

## Code quality

- **Ruff** for formatting and linting, **pyright** for type checking.
- These run automatically via PostToolUse hook on every Edit/Write of a Python file.
- To run manually: `uv run ruff check .` and `uv run pyright`.
- All Python scripts must use `uv run --script` with inline dependency metadata (PEP 723).

## Validation checklist (every change)

- **Manifests**: `uv run scripts/sync_manifests.py --check` must pass.
- **Skills**: `uv run scripts/validate_skills.py` must pass.
- **Linting**: `uv run ruff check .` must pass with no errors.
- **Formatting**: `uv run ruff format --check .` must pass.
- **Type checking**: `uv run pyright` must pass.
- All must pass before claiming any change is complete.

## Documentation

- For every change, assess whether documentation needs updating (README, skill
  descriptions, `AGENTS.md`, inline docs).
- If docs are affected, update them as part of the same change — do not defer.

## Development workflow

- Use `/brainstorm` to explore requirements and write plans. Always use **Opus model**
  with **max effort** for brainstorming (`claude --model opus --effort max`).
- Execute plans with `/workflow` to orchestrate multi-step implementation.

## Review

- Depending on complexity, include relevant review steps in the workflow: `/simplify`,
  `/code-review:code-review`, `/security-review`, doc updates, etc.
- For small changes a single review pass may suffice; for larger changes, combine
  multiple review steps.

## Issue tracking

- Issues are managed in **GitHub Issues** on this repository.
- Whenever you spot something that could be improved (code, docs, tooling, workflow),
  create a GitHub issue to track it.

## Writing skills

- The frontmatter `description` decides **when** an agent loads the skill. Name symptoms
  and trigger phrases, not just the topic. Max 1024 characters.
- Keep `SKILL.md` scannable; put depth in `references/`, which load on demand.
- Prefer a runnable check over a paragraph of prose.

## Installing

**Claude Code**

```bash
claude plugin marketplace add yorrick/agent-skills
claude plugin install <name>@yorrick
```

**Codex**

```bash
codex plugin marketplace add yorrick/agent-skills
codex plugin add <name>@yorrick
```

Both harnesses auto-update, differently. Claude Code's is **opt-in per marketplace** (off
by default for third-party), fetches ~10 min after session start, and updates marketplace
metadata only — the installed plugin still needs `plugin update`. Codex's is **always on
and undocumented**: a `plugins-marketplace-auto-upgrade` thread runs at app-server startup
(the TUI and `codex exec` embed the app-server, so those fire it too), checks each git
marketplace's remote revision, and reinstalls the snapshot whenever it moved — gated only
on `plugins_enabled`. `check_for_update_on_startup` does **not** affect it; that flag only
governs Codex's own self-update prompt (and the desktop app ignores it even there,
[codex#18543](https://github.com/openai/codex/issues/18543), closed as not planned).

Manually: Claude Code needs two steps (`plugin marketplace update yorrick`, then
`plugin update <name>@yorrick`); Codex does both with `codex plugin marketplace upgrade`,
despite its help text saying only "snapshots". Restart afterwards — Codex may serve a
stale skill on a *resumed* thread ([codex#16607](https://github.com/openai/codex/issues/16607)).

Check `ls ~/.codex/plugins/cache/yorrick/<name>/` rather than `plugin list`: the cache is
what the model loads, `list` only reflects config.

Because Codex reinstalls from `main` unprompted, a push here lands on every installed
machine with no review step. Treat this branch as a release channel.
