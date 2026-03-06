# claude-dev-loop

A Claude Code plugin that automates the full feature development lifecycle: brainstorm, plan, implement, create PR, then iteratively review until clean.

## How it works

```
/dev-loop
    |
    v
Phase 1: Interactive brainstorming + planning (you participate)
    |
    v
Phase 2: Automated loop (script takes over)
    |
    +-> Implement plan (with lint, typecheck, format, tests)
    +-> Create PR
    |
    +-> Review loop (repeats until clean or max iterations):
        +-> /simplify
        +-> /code-review + /security-review (parallel)
        +-> Decision: Critical/Important issues?
            yes -> fix issues, loop back
            no  -> done, PR is ready
```

## Prerequisites

This plugin composes several built-in and plugin-provided commands:

- `/simplify` — built-in or from a plugin
- `/code-review:code-review` — from the [code-review plugin](https://github.com/anthropics/claude-code-plugins)
- `/security-review` — built-in
- `superpowers:brainstorming`, `superpowers:writing-plans`, `superpowers:executing-plans` — from the [superpowers plugin](https://github.com/obra/superpowers)
- `gh` CLI — for creating PRs

## Installation

```bash
claude plugin add yorrickjansen/claude-dev-loop
```

Or add to your `.claude/plugins.json`:

```json
{
  "plugins": ["yorrickjansen/claude-dev-loop"]
}
```

## Commands

### `/dev-loop`

Full lifecycle: interactive brainstorming and planning, then automated implementation and review loop.

```
/dev-loop
/dev-loop --max-iterations 5
/dev-loop --skip-permissions
```

**Options:**
- `--max-iterations N` — Max review loop iterations (default: 3)
- `--skip-permissions` — Run headless sessions with bypassPermissions mode

### `/review-loop`

Skip brainstorming and implementation — run the review loop on an existing PR.

```
/review-loop docs/plans/2026-03-06-auth.md --pr-url https://github.com/org/repo/pull/42
/review-loop docs/plans/2026-03-06-auth.md --pr-url https://github.com/org/repo/pull/42 --max-iterations 5
```

### Standalone script

You can also run the orchestrator script directly:

```bash
# Full cycle from a plan
~/.claude/plugins/*/dev-loop/scripts/dev-loop.sh docs/plans/2026-03-06-auth.md

# Review loop on existing PR
~/.claude/plugins/*/dev-loop/scripts/dev-loop.sh docs/plans/2026-03-06-auth.md --pr-url https://github.com/org/repo/pull/42

# See all options
~/.claude/plugins/*/dev-loop/scripts/dev-loop.sh --help
```

## How the review loop works

Each iteration of the review loop spawns separate `claude -p` sessions:

1. **Simplify** — runs `/simplify` to clean up code, commits fixes
2. **Code review** — runs `/code-review:code-review` on the PR (parallel)
3. **Security review** — runs `/security-review` (parallel)
4. **Decision gate** — asks Claude if there are Critical/Important issues
5. **Fix** — if issues found, fixes them and runs quality gates again

The loop exits when either no Critical/Important issues remain or max iterations is reached.

All intermediate outputs are saved to a temp directory for inspection.

## License

MIT
