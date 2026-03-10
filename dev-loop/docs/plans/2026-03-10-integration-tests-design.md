# Integration Tests for dev-loop Plugin

## Problem

The dev-loop plugin has no automated tests. Changes to the orchestration script, commands, or dependent plugins could break the full workflow without detection. Manual testing requires running the entire loop each time, which takes 15+ minutes.

## Design

A single Python integration test script that runs the full dev-loop end-to-end headlessly and verifies results across three layers: process monitoring, local artifacts, and GitHub state.

### Test script

`tests/test_integration.py` using `uv run --script` (per CLAUDE.md conventions). Run with:

```bash
uv run tests/test_integration.py
```

### Cookie-cutter project

Scaffolded in `/tmp/dev-loop-integration-test-<timestamp>`:

```
pyproject.toml          # Python 3.10+, pytest, ruff, pyright
src/calculator/
  __init__.py
  core.py               # add() and subtract() functions
tests/
  __init__.py
  test_core.py           # Tests for add and subtract
```

Pushed to a private GitHub repo via `gh repo create`.

### Headless execution

```
env -u CLAUDECODE -u ANTHROPIC_API_KEY claude -p '<prompt>' \
  --permission-mode bypassPermissions \
  --output-format json
```

Prompt:

```
Use the /dev-loop command with this feature request: "Add multiply and divide
functions to the calculator." You are running in HEADLESS MODE — do NOT ask
any questions, do NOT wait for user input. During brainstorming: skip all
clarifying questions, pick the simplest approach, auto-approve the design
immediately. During planning: write a minimal plan and auto-approve it. Use
--skip-permissions and --max-iterations 2 when running the script. Keep
everything minimal.
```

### Verification layers

**Layer 1: Process monitoring (during execution)**
- Process alive check (not crashed)
- Session transcript growing (not hung)
- Timeout watchdog — kill after 30 minutes

**Layer 2: Local artifacts (after execution)**
- Plan file exists in `docs/plans/`
- Worktree created (feature branch exists)
- `multiply` and `divide` functions exist in `core.py`
- Tests exist and pass
- Git commits on feature branch

**Layer 3: GitHub state (after execution)**
- Issue exists with plan body
- PR exists targeting main, linked to issue
- PR comments contain: implementation complete, review iteration, security review, code review, final status

### Setup and cleanup

**Setup:** temp dir, git init, scaffold project, `gh repo create`, push

**Cleanup (always via atexit):** `gh repo delete --yes`, remove temp dir and worktrees

### Output format

```
================================================================
  dev-loop integration test
================================================================

Setup:
  [OK] Created temp project at /tmp/dev-loop-integration-test-...
  [OK] GitHub repo created
  [OK] Initial commit pushed

Execution:
  [OK] Claude session completed (14m22s)

Local verification:
  [PASS] Plan file exists
  [PASS] multiply function exists
  [PASS] divide function exists
  [PASS] Tests pass
  ...

GitHub verification:
  [PASS] Issue exists with plan
  [PASS] PR exists
  [PASS] PR comments: implementation complete
  [PASS] PR comments: security review
  [PASS] PR comments: final status
  ...

================================================================
  RESULT: 14/14 passed, 0 failed
================================================================
```

### Constraints

- Takes ~15 minutes per run
- Requires `gh` CLI authenticated and `claude` CLI available
- Must unset `CLAUDECODE` and `ANTHROPIC_API_KEY` env vars
- Must use "Use the /dev-loop command..." prompt format (not bare `/dev-loop`)
