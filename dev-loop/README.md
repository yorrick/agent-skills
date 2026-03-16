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
    +-> Smoke test (verify app works locally using plan's Validation section)
    +-> Create PR (or push to existing PR with --continue-pr)
    |
    +-> Review loop (repeats until clean or max iterations):
        +-> /simplify
        +-> /code-review + /security-review (parallel)
        +-> Wait for CI/CD checks
        +-> Decision: Critical/Important issues?
            yes -> fix issues (+ re-run smoke test), loop back
            no  -> done, PR is ready
```

## Prerequisites

This plugin composes several built-in and plugin-provided commands:

- `/simplify` — from the [code-simplifier plugin](https://github.com/anthropics/claude-code-plugins)
- `/code-review:code-review` — from the [code-review plugin](https://github.com/anthropics/claude-code-plugins)
- `/security-review` — built-in
- `superpowers:brainstorming`, `superpowers:writing-plans`, `superpowers:executing-plans` — from the [superpowers plugin](https://github.com/obra/superpowers)
- `gh` CLI — for creating PRs and interacting with GitHub
- `uv` — for running Python scripts ([install](https://docs.astral.sh/uv/))

## Installation

```bash
claude plugin install dev-loop@yorrick
```

## Commands

### `/dev-loop`

Full lifecycle: interactive brainstorming and planning, then automated implementation and review loop.

```
/dev-loop add user authentication
```

**Script options (passed during Phase 2 handoff):**
- `--max-iterations N` — Max review loop iterations (default: 3)
- `--skip-permissions` — Run headless sessions with bypassPermissions mode
- `--continue-pr` — Use current branch and existing PR instead of creating a new worktree and PR
- `--reviewers user1,user2` — Request review from GitHub users or teams
- `--review-only <pr-url>` — Skip implementation, run review loop only

### `/review-loop`

Skip brainstorming and implementation — run the review loop on an existing PR.

```
/review-loop <issue-url> --review-only <pr-url>
/review-loop <issue-url> --review-only <pr-url> --max-iterations 5
```

### `/workflow`

Generate and run an ad-hoc workflow from a natural language description. The agent writes a Python script using the `StateGraph` engine and executes it.

```
/workflow iterate until the tests pass
/workflow lint, typecheck, and test in parallel
```

Generated scripts automatically support a `--diagram` flag that prints a Mermaid flowchart of the workflow graph instead of executing it. The agent always shows the diagram first so you can review the workflow before it runs.

### Standalone script

You can also run the orchestrator script directly:

```bash
# Full cycle (creates worktree + PR)
uv run scripts/dev-loop.py https://github.com/org/repo/issues/42

# Continue on existing branch/PR
uv run scripts/dev-loop.py https://github.com/org/repo/issues/42 --continue-pr

# Review loop only
uv run scripts/dev-loop.py https://github.com/org/repo/issues/42 --review-only https://github.com/org/repo/pull/43

# See all options
uv run scripts/dev-loop.py --help
```

## Monitoring

Monitor progress from another terminal while the script runs:

```bash
# One-line status
watch -n1 cat .dev-loop/latest/status.txt

# Full log
tail -f .dev-loop/latest/dev-loop.log
```

## Local development

### Setup

```bash
git clone git@github.com:yorrick/claude-code-plugins.git
cd claude-code-plugins/dev-loop
```

### Code quality

Ruff for linting/formatting, pyright for type checking:

```bash
cd claude-code-plugins  # run from monorepo root
uv run ruff check dev-loop/scripts/ dev-loop/tests/
uv run pyright dev-loop/scripts/ dev-loop/tests/
```

### Running engine unit tests

```bash
uv run pytest dev-loop/tests/test_engine.py -v
```

### Running integration tests

The integration test creates a temporary GitHub repo, runs the full dev-loop, and verifies the results. It takes ~30-45 minutes and uses your `gh` CLI credentials.

```bash
uv run dev-loop/tests/test_integration.py
```

Add `--no-cleanup` to keep the temporary GitHub repo and local files for debugging:

```bash
uv run dev-loop/tests/test_integration.py --no-cleanup
```

### Project structure

```
dev-loop/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest (name, version)
├── commands/
│   ├── dev-loop.md           # /dev-loop command (orchestrates full lifecycle)
│   ├── review-loop.md        # /review-loop command (review only)
│   └── workflow.md           # /workflow command (ad-hoc workflow generation)
├── scripts/
│   ├── engine.py             # Async graph execution engine (StateGraph, node helpers)
│   └── dev-loop.py           # Main orchestrator script (defines workflow as a graph)
├── tests/
│   ├── test_engine.py        # Unit tests for the workflow engine
│   └── test_integration.py   # End-to-end integration test
├── docs/
│   └── plans/                # Design and implementation plan documents
├── CLAUDE.md                 # Development guidelines for Claude
└── README.md
```

### Architecture

The orchestrator (`dev-loop.py`) defines its workflow as a `StateGraph` powered by `engine.py`:

- **engine.py** — A lightweight LangGraph-inspired async graph execution engine with:
  - `StateGraph` builder: `add_node()`, `add_edge()`, `add_conditional_edges()`, `add_parallel_edges()`
  - Parallel execution via `asyncio.gather()` with join semantics
  - Loop detection with configurable `max_iterations` safety valve
  - Event callbacks (`on_node_start`, `on_node_end`, `on_error`) for observability
  - CLI node helpers: `claude_node()`, `codex_node()`, `gemini_node()`, `python_node()`, `shell_node()`, `template_node()`
  - Mermaid diagram generation via `to_mermaid()` for workflow visualization

- **dev-loop.py** — Defines the workflow graph with nodes wrapping `run_claude()` calls and routers for conditional branching (smoke test pass/fail, decision gate fix/done)

### Publishing a new version

1. Update the version in `.claude-plugin/plugin.json`
2. Commit and push to `main`
3. The plugin registry picks up the new version automatically

## License

MIT
