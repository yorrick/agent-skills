# Align /workflow Command Prompts with dev-loop Context Patterns

**Issue:** #10
**Date:** 2026-03-16

## Problem

The `/workflow` skill's Example 4 (full review loop) uses generic, simplified prompts that don't teach the patterns that make `dev-loop.py` effective. When `/workflow` generates a review loop, it produces prompts that review raw files instead of PR diffs, don't track previous findings, don't post PR comments, and use regex-based decision gates instead of LLM evaluation.

## Approach

Approach B: Rewrite Example 4 to match dev-loop.py's full pipeline, and add a new "Context patterns for review workflows" reference section that documents the principles behind the prompts. Also update the integration test to match.

## Deliverables

### 1. New "Context patterns for review workflows" section in workflow.md

Placed between the "Example workflows" heading and Example 1. Documents six principles distilled from dev-loop.py:

1. **Review the PR diff, not raw files** — Use `/code-review:code-review {pr_url}` and `/security-review` skills, which examine what changed rather than scanning entire files. This catches regressions and avoids noise from pre-existing code.

2. **Track previous findings across iterations** — Carry `previous_security_findings` as a separate state key. The security review prompt instructs the reviewer to (a) check whether previous issues have been resolved, and (b) perform a full new review since fixes may introduce new issues.

3. **Post findings as PR comments** — Review nodes post findings via `gh pr comment {pr_number}`. This creates an audit trail visible to humans and other tools without reading workflow state.

4. **Use an LLM for the decision gate** — Don't regex-match review output to decide whether fixes are needed. Use `claude_node` with sonnet/low to evaluate findings and answer YES/NO. Only Critical/Important/Medium severity triggers a fix; Low severity and nitpicks are skipped.

5. **Run quality gates after every fix** — Every fix prompt includes instructions to run the project's lint, typecheck, format, and test suite, and fix any failures before committing. This prevents fix iterations from introducing new problems.

6. **Smoke test before creating the PR** — Verify the implementation actually works before entering the review loop. Look for a "## Validation" section in the plan; fall back to convention-based discovery (README, package.json, docker-compose.yml, etc.).

### 2. Rewritten Example 4 in workflow.md

**Title:** "Full pipeline: implement → smoke test → PR → simplify → review loop"

**Graph structure:**
```
implement → smoke_test → [pass/fail router]
  fail → smoke_test_fix → smoke_test_retry → [abort (END) or continue]
  pass → create_pr → simplify → simplify_commit → parallel(code_review, security_review)
           → wait_for_ci → decision
               decision(issues) → fix → simplify (back to review loop)
               decision(clean) → END
```

**Node details:**

| Node | Type | Model/Effort | Prompt source | Key differences from current Example 4 |
|------|------|-------------|---------------|----------------------------------------|
| `implement` | `codex_node` / `claude_node` | opus/high | Fetches plan from `{issue_url}`, runs quality gates | Unchanged conceptually, model upgraded to opus/high |
| `smoke_test` | `claude_node` | opus/high | Looks for Validation section in plan, falls back to convention-based discovery. Ends with `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL: <summary>` | **New step** |
| `smoke_test_fix` | `claude_node` | opus/high | Receives `{smoke_test_output}`, diagnoses root cause, fixes code, runs quality gates | **New step** |
| `smoke_test_retry` | `python_node` wrapping `claude_node` | opus/high | Re-runs smoke test after fix. Sets `smoke_test_retry_failed` flag on persistent failure | **New step** |
| `create_pr` | `claude_node` | sonnet/low | Pushes branch, creates PR via `gh pr create` with descriptive title/body, returns `pr_url` | **New step** — LLM generates PR title/body |
| `simplify` | `claude_node` | sonnet/high | Runs `/simplify` skill | **New step** |
| `simplify_commit` | `claude_node` | sonnet/low | "If there are uncommitted changes from the simplify pass, commit and push" | **New step** — LLM-driven commit, not raw shell |
| `code_review` | `claude_node` | opus/high | `/code-review:code-review {pr_url}` — reviews the PR diff | Changed from file-level review, model upgraded to opus/high |
| `security_review` | `claude_node` | opus/high | `/security-review` with `{pr_url}`, carries `{previous_security_findings}`, posts findings via `gh pr comment` | Changed from file-level review, adds finding tracking and PR comments |
| `wait_for_ci` | `python_node` | N/A | Polls `gh pr checks {pr_number}` until CI completes. Returns `ci_status` (pass/fail) and `ci_failures` | **New step** — waits for CI before decision |
| `decision` | `python_node` wrapping `claude_node` | sonnet/low | Short-circuits to YES on CI failure. Otherwise receives `{code_review_output}` + `{security_review_output}` + `{ci_failures}`, answers YES/NO. Carries `previous_security_findings` into state | Changed from `python_node` regex to LLM evaluation with CI awareness |
| `fix` | `claude_node` | opus/high | Receives review findings + `{pr_url}` + `{ci_failures}`, fixes Critical/Important/Medium issues, runs quality gates, commits and pushes | Changed to include PR context, CI failures, and quality gates |

**State keys:**
- `work_dir`, `issue_url` — input
- `impl_output`, `smoke_test_output`, `smoke_test_error` — implementation phase
- `smoke_test_retry_failed` — set to `"true"` by `smoke_test_retry` on persistent failure
- `pr_url` — created by `create_pr`
- `code_review_output`, `security_review_output` — review findings
- `ci_status`, `ci_failures` — from `wait_for_ci` node
- `previous_security_findings` — carried across iterations by decision node
- `decision_output` — YES/NO from decision gate
- `iteration_count` — tracks review loop iterations

**Routers:**
- `smoke_test_router`: checks `smoke_test_error` (runtime error) or `SMOKE_TEST_FAIL` in `smoke_test_output` → `fail` or `pass`
- `post_retry_router`: checks if retry failed → `abort` (END) or `continue` (create_pr)
- `decision_router`: checks for `YES` in decision output → `fix` or `done` (END)

**`python_node` wrappers needed for:**
- `decision` node: short-circuits on CI failure, carries `previous_security_findings` into state, increments `iteration_count`
- `smoke_test_retry`: sets `smoke_test_retry_failed` flag on persistent failure
- `wait_for_ci`: polls `gh pr checks` until CI completes, returns `ci_status` and `ci_failures`

### 3. Updated integration test (test_workflow_integration.py)

The integration test generates a workflow script and runs it end-to-end. Updates:

- **Generated script** matches new Example 4 structure (smoke test, PR creation, simplify, diff-based reviews, decision gate, previous findings tracking)
- **Assertions** verify new nodes appear in progress logs (`smoke_test`, `create_pr`, `simplify`, `code_review`, `security_review`, `decision`)
- **Existing scaffolding** (temp project, git init, cleanup) stays the same
- Test continues to require Codex CLI and network access (already the case); `gh` CLI is also required (already available in CI)

## Intentional simplifications vs. dev-loop.py

Example 4 teaches the patterns but is not a 1:1 clone of dev-loop.py. These are intentionally omitted:

- **No worktree setup** — dev-loop.py creates an isolated git worktree. Example 4 works in the current directory for simplicity.
- **No `--continue-pr` mode** — dev-loop.py supports resuming with an existing PR via `continue_pr_push`. Example 4 always creates a fresh PR.
- **No `RunContext` / file artifact writing** — dev-loop.py writes per-step JSON artifacts to `.dev-loop/runs/`. Example 4 uses the engine's default progress logging.
- **No desktop notifications** — dev-loop.py sends notifications via `_ctx.notify()`. Example 4 skips this.

## What does NOT change

- **Examples 1-3** in workflow.md — they demonstrate simpler patterns and don't need review loop context patterns
- **API reference, node types, model selection guide** — unchanged
- **"Important rules" section** — unchanged
- **Engine code (engine.py)** — no changes
- **dev-loop.py** — no changes, it's the source of truth we're aligning toward

## Risks

- **Example 4 length increases** from ~60 lines to ~150-180 lines. Acceptable because it's the canonical full-pipeline example, and the added complexity is real (not padding).
- **Integration test becomes more complex** with more nodes to verify. Mitigated by the test already having the infrastructure for multi-node workflows.
