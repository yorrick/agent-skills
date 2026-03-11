# Smoke Test Step Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a smoke test step (Phase 1.5) to the dev-loop that runs the application locally after implementation to verify it actually works before creating a PR.

**Architecture:** Two new prompt functions (`_smoke_test_prompt`, `_smoke_test_fix_prompt`) plus a Phase 1.5 block in `main()` that sits between implementation and PR creation. The existing `_fix_prompt` gains an `issue_url` parameter so review-loop fixes also re-run smoke tests. Commands markdown updated to document the new step.

**Tech Stack:** Python 3.10+, uv inline deps (PEP 723), claude CLI headless sessions

**Spec:** `dev-loop/docs/plans/2026-03-11-smoke-test-design.md`

---

## Chunk 1: Core prompt functions and Phase 1.5 logic

### Task 1: Add `_smoke_test_prompt()` function

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py:553` (insert before `_fix_prompt`)

- [ ] **Step 1: Write the `_smoke_test_prompt` function**

Insert this function before `_fix_prompt` (after line 551, the end of `_decision_prompt`):

```python
def _smoke_test_prompt(issue_url: str) -> str:
    issue_number = extract_issue_number(issue_url)
    return (
        f"Run a smoke test to verify the implementation actually works.\n\n"
        f"1. Fetch the implementation plan from GitHub issue {issue_url} using:\n"
        f"   gh issue view {issue_number} --json body --jq .body\n\n"
        "2. Look for a 'Validation' section (## Validation header) in the plan.\n\n"
        "3. If a Validation section exists, execute those validation instructions exactly:\n"
        "   - Start any long-running processes (servers, etc.) in the background\n"
        "   - Use a non-standard port (e.g., 8099) to avoid conflicts\n"
        "   - Wait up to 30 seconds for the service to be ready (poll with curl or similar)\n"
        "   - Run each specified check\n"
        "   - ALWAYS kill all background processes before exiting, even on failure\n"
        "   - Report pass/fail for each check\n\n"
        "4. If NO Validation section exists, fall back to convention-based discovery:\n"
        "   - Read README.md, CLAUDE.md, pyproject.toml, package.json, Makefile, docker-compose.yml\n"
        "   - Figure out how to run the application locally\n"
        "   - Perform a basic sanity check: does it start? Does --help work? "
        "Does a health endpoint respond?\n"
        "   - ALWAYS kill all background processes before exiting\n\n"
        "5. End your response with EXACTLY one of these lines (no extra text after it):\n"
        "   SMOKE_TEST_PASS\n"
        "   SMOKE_TEST_FAIL: <brief summary of what failed>\n\n"
        "IMPORTANT: You MUST clean up all background processes before finishing. "
        "Use 'kill %1' or 'kill $PID' to stop any servers you started."
    )
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -c "import ast; ast.parse(open('dev-loop/scripts/dev-loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "feat(dev-loop): add _smoke_test_prompt function"
```

### Task 2: Add `_smoke_test_fix_prompt()` function

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py` (insert right after `_smoke_test_prompt`)

- [ ] **Step 1: Write the `_smoke_test_fix_prompt` function**

Insert immediately after `_smoke_test_prompt`:

```python
def _smoke_test_fix_prompt(issue_url: str, smoke_test_output: str) -> str:
    issue_number = extract_issue_number(issue_url)
    return (
        "The smoke test failed. Fix the code so the application works correctly.\n\n"
        f"Smoke test output:\n{smoke_test_output}\n\n"
        f"For context, fetch the implementation plan from GitHub issue {issue_url} using:\n"
        f"  gh issue view {issue_number} --json body --jq .body\n\n"
        "Diagnose the root cause from the error output above, fix the code, "
        "then run the project's quality gates (lint, typecheck, format, tests) "
        "to make sure your fixes don't break anything.\n\n"
        "Commit the fixes locally. Do NOT push."
    )
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -c "import ast; ast.parse(open('dev-loop/scripts/dev-loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "feat(dev-loop): add _smoke_test_fix_prompt function"
```

### Task 3: Update `_fix_prompt()` to accept `issue_url` and update call site

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py` (the `_fix_prompt` function AND its call site in the review loop)

IMPORTANT: Both the signature change and the call site update MUST be done in the same commit to avoid a regression where `ci_failures` is silently passed as `issue_url`.

- [ ] **Step 1: Update `_fix_prompt` signature and body**

Change the existing `_fix_prompt` function from:

```python
def _fix_prompt(pr_url: str, code_review_text: str, security_review_text: str, ci_failures: str = "") -> str:
    parts = [
        f"The following issues were found during review of PR {pr_url}. "
        "Fix all Critical, Important, and Medium severity issues. After fixing, run the project's "
        "quality gates (lint, typecheck, format, tests) and make sure everything "
        "passes. Commit and push the fixes.\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}"
    ]
    if ci_failures:
        parts.append(f"\n\nCI/CD failures (MUST fix):\n{ci_failures}")
    return "".join(parts)
```

To:

```python
def _fix_prompt(
    pr_url: str,
    code_review_text: str,
    security_review_text: str,
    issue_url: str = "",
    ci_failures: str = "",
) -> str:
    parts = [
        f"The following issues were found during review of PR {pr_url}. "
        "Fix all Critical, Important, and Medium severity issues. After fixing, run the project's "
        "quality gates (lint, typecheck, format, tests) and make sure everything "
        "passes. Commit and push the fixes.\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}"
    ]
    if ci_failures:
        parts.append(f"\n\nCI/CD failures (MUST fix):\n{ci_failures}")
    if issue_url:
        issue_number = extract_issue_number(issue_url)
        parts.append(
            "\n\nAfter fixing all issues and running quality gates, re-run the smoke test validation. "
            f"Fetch the plan from issue {issue_url} using:\n"
            f"  gh issue view {issue_number} --json body --jq .body\n\n"
            "Look for the Validation section. Execute the validation checks. "
            "If any long-running processes are needed (servers, etc.), "
            "start them in the background on a non-standard port (e.g., 8099), "
            "run the checks, and kill them before finishing. "
            "If smoke test checks fail, fix those too before committing."
        )
    return "".join(parts)
```

- [ ] **Step 2: Update the `_fix_prompt` call site in the review loop**

In the review loop's fix step, change:

```python
        run_claude(
            _fix_prompt(pr_url, code_review_text, security_review_text, ci_failures),
            work_dir / f"fix-{iteration}.json",
```

To:

```python
        run_claude(
            _fix_prompt(pr_url, code_review_text, security_review_text, issue_url=issue_url, ci_failures=ci_failures),
            work_dir / f"fix-{iteration}.json",
```

- [ ] **Step 3: Verify the file still parses**

Run: `python -c "import ast; ast.parse(open('dev-loop/scripts/dev-loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit both changes together**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "feat(dev-loop): update _fix_prompt to accept issue_url and update call site"
```

### Task 4: Add Phase 1.5 smoke test block in `main()` — default mode

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py` (in the `elif not pr_url:` branch of `main()`, between implementation and PR creation)

- [ ] **Step 1: Insert Phase 1.5 block after implementation, before PR creation**

In the `elif not pr_url:` branch, after the implementation error check (after the `return 1` for implementation failure, around line 697), and BEFORE the `ctx.status("Phase 1b", "Creating PR")` line (around line 699), insert:

```python
        # --- Phase 1.5: Smoke test ---
        ctx.status("Phase 1.5", "Smoke test")
        ctx.log("PHASE 1.5: Smoke test")
        smoke_file = run_claude(
            _smoke_test_prompt(issue_url),
            work_dir / "smoke-test.json",
            permission_mode,
            cwd=worktree_path,
            model="opus",
            effort="high",
        )
        err = check_claude_error(smoke_file)
        smoke_result = extract_result(smoke_file) if not err else ""

        if err or "SMOKE_TEST_FAIL" in smoke_result:
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
                return 1

        ctx.log("PHASE 1.5: Smoke test PASSED")
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -c "import ast; ast.parse(open('dev-loop/scripts/dev-loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "feat(dev-loop): add Phase 1.5 smoke test for default mode"
```

### Task 5: Add Phase 1.5 smoke test block in `main()` — continue-pr mode

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py` (in the `if args.continue_pr:` branch of `main()`, between implementation and push)

- [ ] **Step 1: Insert Phase 1.5 block after implementation, before push**

In the `if args.continue_pr:` branch, after the implementation error check (after the `return 1` for implementation failure, around line 643), and BEFORE the `ctx.status("Phase 1b", "Pushing commits (continue-pr)")` line (around line 645), insert:

```python
        # --- Phase 1.5: Smoke test ---
        ctx.status("Phase 1.5", "Smoke test (continue-pr)")
        ctx.log("PHASE 1.5: Smoke test (continue-pr)")
        smoke_file = run_claude(
            _smoke_test_prompt(issue_url),
            work_dir / "smoke-test.json",
            permission_mode,
            cwd=None,
            model="opus",
            effort="high",
        )
        err = check_claude_error(smoke_file)
        smoke_result = extract_result(smoke_file) if not err else ""

        if err or "SMOKE_TEST_FAIL" in smoke_result:
            ctx.status("Phase 1.5", "Fixing smoke test failures (continue-pr)")
            ctx.log("PHASE 1.5: Smoke test FAILED, running fix cycle")
            run_claude(
                _smoke_test_fix_prompt(issue_url, smoke_result or err),
                work_dir / "smoke-test-fix.json",
                permission_mode,
                cwd=None,
                model="opus",
                effort="high",
            )

            ctx.status("Phase 1.5", "Smoke test retry (continue-pr)")
            ctx.log("PHASE 1.5: Smoke test retry")
            smoke_retry_file = run_claude(
                _smoke_test_prompt(issue_url),
                work_dir / "smoke-test-retry.json",
                permission_mode,
                cwd=None,
                model="opus",
                effort="high",
            )
            retry_err = check_claude_error(smoke_retry_file)
            retry_result = extract_result(smoke_retry_file) if not retry_err else ""

            if retry_err or "SMOKE_TEST_FAIL" in retry_result:
                ctx.status("Error", "Smoke test failed after fix attempt")
                ctx.log("ERROR: Smoke test still failing after fix attempt")
                ctx.notify("dev-loop aborted: smoke test failed after fix attempt")
                return 1

        ctx.log("PHASE 1.5: Smoke test PASSED (continue-pr)")
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -c "import ast; ast.parse(open('dev-loop/scripts/dev-loop.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "feat(dev-loop): add Phase 1.5 smoke test for continue-pr mode"
```

## Chunk 2: Quality gates, command docs, and version bump

### Task 6: Run quality gates on dev-loop.py

**Files:**
- Check: `dev-loop/scripts/dev-loop.py`

- [ ] **Step 1: Run ruff check**

Run: `uv run ruff check dev-loop/scripts/dev-loop.py`
Expected: No errors (or only pre-existing ones)

- [ ] **Step 2: Run pyright**

Run: `uv run pyright dev-loop/scripts/dev-loop.py`
Expected: No new errors

- [ ] **Step 3: Fix any issues found and commit**

If ruff or pyright report new issues, fix them and commit:

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "fix(dev-loop): fix lint/type issues in smoke test code"
```

### Task 7: Update `commands/dev-loop.md`

**Files:**
- Modify: `dev-loop/commands/dev-loop.md`

- [ ] **Step 1: Update the script description to mention smoke test**

In the "The script will:" section (lines 67-77), update to include the smoke test step. Change:

```markdown
The script will:
1. Create a feature branch and worktree (default mode) or use the current branch (--continue-pr)
2. Fetch the plan from the GitHub issue
3. Implement the plan (using executing-plans skill) including running lint, typecheck, format, and tests
4. Create a PR linked to the issue (default mode) or push to the existing PR (--continue-pr)
5. Run a review loop:
```

To:

```markdown
The script will:
1. Create a feature branch and worktree (default mode) or use the current branch (--continue-pr)
2. Fetch the plan from the GitHub issue
3. Implement the plan (using executing-plans skill) including running lint, typecheck, format, and tests
4. Run a smoke test — verify the application starts and works locally using validation instructions from the plan (falls back to convention-based discovery if no validation section exists)
5. Create a PR linked to the issue (default mode) or push to the existing PR (--continue-pr)
6. Run a review loop:
```

- [ ] **Step 2: Add a note about the Validation section in brainstorming guidance**

At the end of the "## Phase 1: Brainstorm (interactive)" section (after line 29), add:

```markdown

During brainstorming, make sure the spec includes a **## Validation** section describing how to verify the feature works locally (e.g., start the server and hit an endpoint, run the CLI with specific args). This is used by the automated smoke test step after implementation.
```

- [ ] **Step 3: Commit**

```bash
git add dev-loop/commands/dev-loop.md
git commit -m "docs(dev-loop): document smoke test step in dev-loop command"
```

### Task 8: Update `commands/review-loop.md`

**Files:**
- Modify: `dev-loop/commands/review-loop.md`

- [ ] **Step 1: Add smoke test mention to the script description**

After line 19 ("4. If Critical/Important issues or CI failures found: fix and loop"), update to mention that fix cycles also re-run smoke tests. Change:

```markdown
4. If Critical/Important issues or CI failures found: fix and loop
5. If clean and CI passing: done
```

To:

```markdown
4. If Critical/Important issues or CI failures found: fix (including re-running smoke test validation from the plan) and loop
5. If clean and CI passing: done
```

- [ ] **Step 2: Commit**

```bash
git add dev-loop/commands/review-loop.md
git commit -m "docs(dev-loop): mention smoke test re-runs in review-loop command"
```

### Task 9: Bump version in plugin.json

**Files:**
- Modify: `dev-loop/.claude-plugin/plugin.json`

- [ ] **Step 1: Bump version from 0.19.0 to 0.20.0**

Change `"version": "0.19.0"` to `"version": "0.20.0"` in `dev-loop/.claude-plugin/plugin.json`.

- [ ] **Step 2: Commit**

```bash
git add dev-loop/.claude-plugin/plugin.json
git commit -m "chore: bump dev-loop version to 0.20.0"
```

### Task 10: Run full quality gates and verify

**Files:**
- Check: all modified files

- [ ] **Step 1: Run ruff check on the whole project**

Run: `uv run ruff check .`
Expected: Clean (or only pre-existing warnings)

- [ ] **Step 2: Run pyright**

Run: `uv run pyright`
Expected: No new errors

- [ ] **Step 3: Verify the script's --help still works**

Run: `uv run dev-loop/scripts/dev-loop.py --help`
Expected: Help text prints successfully with no errors

- [ ] **Step 4: Run the integration test**

Run: `uv run dev-loop/tests/test_integration.py`

Note: This is required by the project's CLAUDE.md after any dev-loop change. The integration test runs the full dev-loop end-to-end, creates a real GitHub repo, and verifies the results. It can take 15-45 minutes. If time-constrained, confirm with the user before running.
