# Smoke Test Step for dev-loop

## Problem

The dev-loop currently validates code through static analysis (lint, typecheck, format) and unit tests during implementation, then reviews code quality and security during the review loop. But it never actually **runs the application** to verify it works. A FastAPI server might pass all tests yet fail to start due to a missing import at module level. A CLI might pass linting but crash on its first invocation.

There is no step that provides the guarantee: "the thing we built actually starts and does what the spec says."

## Design

Two coordinated changes:

1. **Spec-level validation instructions** — the brainstorming/spec phase produces a "Validation" section that describes how to verify the feature works locally.
2. **Dedicated smoke test step in dev-loop.py** — a new phase that runs after implementation and before PR creation/push, executing the validation instructions as a hard gate.

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

**Placement:** After implementation (Phase 1), before PR creation/push (Phase 1b). This applies to **all modes**:

```
Default mode:
  Phase 1: Implementation → Phase 1.5: Smoke test → Phase 1b: PR creation

--continue-pr mode:
  Phase 1: Implementation → Phase 1.5: Smoke test → Phase 1b: Push + detect PR

--review-only mode:
  (no smoke test — enters review loop directly, smoke test runs as part of fix cycles only)
```

For `--review-only`, the smoke test is skipped at entry since we're reviewing an existing PR. It only runs as part of fix cycles within the review loop (see Section 3).

#### `_smoke_test_prompt(issue_url)`

The prompt instructs Claude to:

1. Fetch the implementation plan from the GitHub issue
2. Look for a "Validation" section (or "## Validation" header) in the plan
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
5. On failure — return the specific failures with error output

The prompt must explicitly state:
- Use non-standard ports (e.g., 8099) to avoid conflicts with running services
- Set a 30-second timeout for readiness polling
- Kill all background processes before exiting, regardless of success or failure
- End the response with exactly `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL` followed by a summary of what failed

#### `_smoke_test_fix_prompt(issue_url, smoke_test_output)`

The fix prompt instructs Claude to:

1. Read the smoke test failure output to understand what broke
2. Fetch the implementation plan from the GitHub issue for context
3. Diagnose the root cause (missing import, wrong config, broken endpoint, etc.)
4. Fix the code
5. Run quality gates (lint, typecheck, format, tests) to ensure fixes don't break static checks
6. Commit the fixes locally (do NOT push)

The prompt receives `smoke_test_output` (the full text output from the failed smoke test session) so Claude has the exact error messages.

#### Phase 1.5 in `main()`

```python
# --- Phase 1.5: Smoke test ---
ctx.status("Phase 1.5", "Smoke test")
ctx.log("PHASE 1.5: Smoke test")
smoke_file = run_claude(
    _smoke_test_prompt(issue_url),
    work_dir / "smoke-test.json",
    permission_mode,
    cwd=worktree_path,  # or None for --continue-pr
    model="opus",
    effort="high",
)
err = check_claude_error(smoke_file)
smoke_result = extract_result(smoke_file) if not err else ""

if err or "SMOKE_TEST_FAIL" in smoke_result:
    # Fix cycle
    ctx.status("Phase 1.5", "Fixing smoke test failures")
    ctx.log("PHASE 1.5: Smoke test FAILED, running fix cycle")
    run_claude(
        _smoke_test_fix_prompt(issue_url, smoke_result or err),
        work_dir / "smoke-test-fix.json",
        permission_mode,
        cwd=worktree_path,
        model="opus",
        effort="high",
    )
    # Retry
    ctx.status("Phase 1.5", "Smoke test retry")
    ctx.log("PHASE 1.5: Smoke test retry")
    smoke_retry_file = run_claude(
        _smoke_test_prompt(issue_url),
        work_dir / "smoke-test-retry.json",
        permission_mode,
        cwd=worktree_path,
        model="opus",
        effort="high",
    )
    retry_err = check_claude_error(smoke_retry_file)
    retry_result = extract_result(smoke_retry_file) if not retry_err else ""

    if retry_err or "SMOKE_TEST_FAIL" in retry_result:
        ctx.status("Error", "Smoke test failed after fix attempt")
        ctx.log("ERROR: Smoke test still failing after fix attempt")
        ctx.notify("dev-loop aborted: smoke test failed after fix attempt")
        # No PR exists yet, so no PR comment to post
        return 1
```

**Abort semantics:** On double failure, the script:
- Returns exit code 1
- Sends a macOS notification ("dev-loop aborted: smoke test failed after fix attempt")
- Logs the failure to the run directory
- Does NOT create a PR (the whole point is to avoid PRs for broken code)
- Does NOT post to GitHub (no PR exists yet to comment on)

### 3. Review loop integration

The fix prompt (`_fix_prompt`) gains an additional paragraph instructing Claude to re-run the smoke test after applying fixes:

```
"After fixing all issues and running quality gates, re-run the smoke test validation. "
"Fetch the plan from issue {issue_url} and look for the Validation section. "
"Execute the validation checks. If any long-running processes are needed (servers, etc.), "
"start them in the background, run the checks, and kill them before finishing. "
"If smoke test checks fail, fix those too before committing."
```

This is folded into the fix prompt (not a separate step) because:
- The fix session already has full context of what was changed
- A separate step would add another Claude session (cost + latency) per review iteration
- If the smoke test fails within the fix session, Claude can fix it immediately in the same session

The `_fix_prompt` function signature changes to accept `issue_url` as an additional keyword parameter:

```python
def _fix_prompt(pr_url: str, code_review_text: str, security_review_text: str,
                issue_url: str = "", ci_failures: str = "") -> str:
```

The call site in the review loop's fix step updates to:

```python
_fix_prompt(pr_url, code_review_text, security_review_text,
            issue_url=issue_url, ci_failures=ci_failures)
```

If `issue_url` is provided and the plan contains a Validation section, the smoke test paragraph is appended to the fix prompt. If `issue_url` is empty (shouldn't happen in practice), the smoke test paragraph is omitted.

If the fix-session smoke test fails, Claude should fix and re-check within the same session. The review loop itself does not retry — if the fix session can't make the smoke test pass, the next review iteration will catch it.

For `--review-only` mode, the fix prompt also includes the smoke test instructions. This means the first time smoke tests run in `--review-only` is during the first fix cycle (if issues are found). If no issues are found, no smoke test runs — which is acceptable since the PR already exists and passed its original smoke test.

### 4. Known limitations

**Orphaned processes:** If the Claude session crashes or times out mid-smoke-test, background processes (e.g., a running server) may be left behind. The prompt instructs Claude to clean up, but this is best-effort. We accept this risk because:
- It's a rare edge case (Claude session crash during the ~30s smoke test window)
- The processes use non-standard ports, so they don't block normal development
- Users can identify and kill them manually if needed
- Adding host-side process management would significantly complicate the script for a marginal improvement

## What this does NOT cover

- **External dependencies** (databases, third-party APIs) — the smoke test runs in local-only mode. If the app needs a database, the validation section should specify how to handle that (e.g., SQLite for local, mock server, or skip those checks).
- **Browser/UI testing** — this is for backend/CLI validation only.
- **Performance testing** — we're checking "does it work", not "is it fast".

## Components to modify

1. **`scripts/dev-loop.py`** — add `_smoke_test_prompt()`, `_smoke_test_fix_prompt()`, update `_fix_prompt()` signature, and add Phase 1.5 block in `main()` (both default and `--continue-pr` paths)
2. **`commands/dev-loop.md`** — update the phase description to mention the smoke test step, and remind users during brainstorming to include a Validation section in their spec
3. **`commands/review-loop.md`** — mention that fixes also re-validate via smoke test

## Implementation approach

Since we can't modify the brainstorming skill directly (it's in the superpowers plugin), we handle validation discovery at two levels:

- **Best case:** The spec/plan already has a Validation section (written during brainstorming). The smoke test prompt uses it directly.
- **Fallback:** No validation section exists. The smoke test prompt uses convention-based discovery to figure out how to run and test the application.

The dev-loop command markdown (`commands/dev-loop.md`) will be updated to remind users during the interactive brainstorming phase to include validation steps in their spec.
