# Smoke Test Step for dev-loop

## Problem

The dev-loop currently validates code through static analysis (lint, typecheck, format) and unit tests during implementation, then reviews code quality and security during the review loop. But it never actually **runs the application** to verify it works. A FastAPI server might pass all tests yet fail to start due to a missing import at module level. A CLI might pass linting but crash on its first invocation.

There is no step that provides the guarantee: "the thing we built actually starts and does what the spec says."

## Design

Two coordinated changes:

1. **Spec-level validation instructions** — the brainstorming/spec phase produces a "Validation" section that describes how to verify the feature works locally.
2. **Dedicated smoke test step in dev-loop.py** — a new phase that runs after implementation and before PR creation, executing the validation instructions as a hard gate.

### 1. Validation section in specs

The brainstorming skill already produces a design spec. We add a required **"## Validation"** section to the spec template. This section describes:

- How to run the application locally (e.g., `uvicorn main:app`, `python cli.py --help`)
- What constitutes a passing sanity check (e.g., server starts, health endpoint returns 200)
- Functional checks tied to the feature (e.g., "POST /users returns 201", "CLI --dry-run exits 0 with expected output")

Example for a FastAPI feature:

```markdown
## Validation

### Sanity check
Start the server with `uvicorn src.main:app --port 8099` and verify it responds:
- `curl http://localhost:8099/health` returns 200

### Functional checks
- `curl -X POST http://localhost:8099/users -d '{"name":"test"}' -H 'Content-Type: application/json'` returns 201
- `curl http://localhost:8099/users` returns a list containing the created user
```

Example for a CLI tool:

```markdown
## Validation

### Sanity check
- `python -m mytool --help` exits 0 and prints usage information

### Functional checks
- `python -m mytool process --dry-run sample.txt` exits 0 and prints expected output
- `python -m mytool validate config.yaml` exits 0 for a valid config
```

This section flows into the GitHub issue (via the plan), making it available to the dev-loop script.

### 2. Smoke test step in dev-loop.py

A new `_smoke_test_prompt()` function and corresponding phase in `main()`.

**Placement:** After implementation (Phase 1), before PR creation (Phase 1b). This becomes Phase 1.5.

**Flow:**

```
Phase 1: Implementation (existing)
    ↓
Phase 1.5: Smoke test (NEW)
    ↓
Phase 1b: PR creation (existing)
    ↓
Phase 2: Review loop (existing)
```

**The prompt instructs Claude to:**

1. Read the implementation plan from the GitHub issue
2. Look for a "Validation" section in the plan
3. If found — execute those validation instructions exactly:
   - Start any long-running processes in the background
   - Wait for readiness (poll port, check health endpoint)
   - Run the specified checks
   - **Always clean up** background processes, even on failure
   - Report pass/fail for each check
4. If no validation section found — fall back to convention-based discovery:
   - Read README, CLAUDE.md, pyproject.toml, package.json, Makefile, docker-compose.yml
   - Identify how to run the application locally
   - Perform a basic sanity check: does it start? Does `--help` work? Does the health endpoint respond?
5. On failure — return the specific failures so they can feed into a fix cycle

**On failure:** The script logs the smoke test failure and runs a fix cycle (reusing the implementation prompt pattern) before retrying the smoke test. If it fails again after the fix, the script aborts — don't create a PR for code that doesn't run.

**Retry logic:**

```
Smoke test → PASS → continue to PR creation
Smoke test → FAIL → fix cycle → Smoke test retry → PASS → continue
Smoke test → FAIL → fix cycle → Smoke test retry → FAIL → abort
```

This gives one chance to fix smoke test failures before giving up. The fix prompt receives the smoke test output so Claude knows what broke.

**Process lifecycle management:** The prompt explicitly instructs Claude to:
- Start servers on non-standard ports (e.g., 8099) to avoid conflicts
- Use `kill` to clean up background processes in a `trap` or finally block
- Set timeouts for readiness checks (max 30 seconds waiting for a server to start)

### 3. Review loop integration

After the fix step (Step 4) in the review loop, the smoke test re-runs before pushing. This ensures fixes from code review don't break the running application.

The fix prompt (`_fix_prompt`) is updated to include: "After fixing and running quality gates, also re-run the smoke test validation from the plan."

This is lighter-weight than a separate step — it's folded into the fix prompt so the same Claude session that applies fixes also validates they work.

## What this does NOT cover

- **External dependencies** (databases, third-party APIs) — the smoke test runs in local-only mode. If the app needs a database, the validation section should specify how to handle that (e.g., SQLite for local, mock server, or skip those checks).
- **Browser/UI testing** — this is for backend/CLI validation only.
- **Performance testing** — we're checking "does it work", not "is it fast".

## Components to modify

1. **`scripts/dev-loop.py`** — add `_smoke_test_prompt()`, `_smoke_test_fix_prompt()`, and the Phase 1.5 block in `main()`
2. **`commands/dev-loop.md`** — update the phase description to mention the smoke test step
3. **`commands/review-loop.md`** — mention that fixes also re-validate via smoke test
4. **Brainstorming skill guidance** — the spec template should prompt for a Validation section (this is in the superpowers plugin, so we document the recommendation but can't modify it directly; instead, the `_implementation_prompt` can remind the implementation agent to include validation steps in the spec)

## Implementation approach

Since we can't modify the brainstorming skill directly (it's in the superpowers plugin), we handle validation discovery at two levels:

- **Best case:** The spec/plan already has a Validation section (written during brainstorming). The smoke test prompt uses it directly.
- **Fallback:** No validation section exists. The smoke test prompt uses convention-based discovery to figure out how to run and test the application.

The dev-loop command markdown (`commands/dev-loop.md`) will be updated to remind users during the interactive brainstorming phase to include validation steps in their spec.
