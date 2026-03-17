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
  fail → smoke_test_fix → smoke_test_retry → [abort or continue]
  pass → create_pr → simplify → simplify_commit → parallel(code_review, security_review) → decision
    decision(issues) → fix → simplify (loop)
    decision(clean) → END
```

**Node details:**

| Node | Type | Prompt source | Key differences from current Example 4 |
|------|------|---------------|----------------------------------------|
| `implement` | `codex_node` / `claude_node` | Fetches plan from `{issue_url}`, runs quality gates | Unchanged conceptually |
| `smoke_test` | `claude_node` | Looks for Validation section in plan, falls back to convention-based discovery. Ends with `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL: <summary>` | **New step** |
| `smoke_test_fix` | `claude_node` | Receives `{smoke_test_output}`, diagnoses root cause, fixes code, runs quality gates | **New step** |
| `smoke_test_retry` | `claude_node` | Re-runs smoke test after fix. On second failure, aborts | **New step** |
| `create_pr` | `shell_node` | `git push -u origin HEAD && gh pr create --title '...' --body '...'` | **New step** — creates PR, returns `pr_url` |
| `simplify` | `claude_node` | Runs `/simplify` skill | **New step** |
| `simplify_commit` | `shell_node` | Commits and pushes simplify changes if any | **New step** |
| `code_review` | `claude_node` | `/code-review:code-review {pr_url}` — reviews the PR diff | Changed from file-level review |
| `security_review` | `claude_node` | `/security-review` with `{pr_url}`, carries `{previous_security_findings}`, posts findings via `gh pr comment` | Changed from file-level review, adds finding tracking and PR comments |
| `decision` | `claude_node` | Receives `{code_review_output}` + `{security_review_output}`, answers YES/NO for Critical/Important/Medium issues | Changed from `python_node` regex to `claude_node` sonnet/low |
| `fix` | `claude_node` | Receives review findings + `{pr_url}`, fixes Critical/Important/Medium issues, runs quality gates, commits and pushes | Changed to include PR context and quality gates |

**State keys:**
- `work_dir`, `issue_url` — input
- `impl_output`, `smoke_test_output` — implementation phase
- `pr_url` — created by `create_pr`
- `code_review_output`, `security_review_output` — review findings
- `previous_security_findings` — carried across iterations by decision node
- `decision_output` — YES/NO from decision gate
- `iteration_count` — tracks review loop iterations

**Routers:**
- `smoke_test_router`: checks for `SMOKE_TEST_FAIL` in output → `fail` or `pass`
- `post_retry_router`: checks if retry failed → `abort` (END) or `continue` (create_pr)
- `decision_router`: checks for `YES` in decision output → `fix` or `done` (END)

**`python_node` wrappers needed for:**
- `decision` node: to carry `previous_security_findings` into state alongside the LLM decision
- `smoke_test_retry`: to set an abort flag on persistent failure

### 3. Updated integration test (test_workflow_integration.py)

The integration test generates a workflow script and runs it end-to-end. Updates:

- **Generated script** matches new Example 4 structure (smoke test, PR creation, simplify, diff-based reviews, decision gate, previous findings tracking)
- **Assertions** verify new nodes appear in progress logs (`smoke_test`, `create_pr`, `simplify`, `code_review`, `security_review`, `decision`)
- **Existing scaffolding** (temp project, git init, cleanup) stays the same
- Test continues to require Codex CLI and network access (already the case); `gh` CLI is also required (already available in CI)

## What does NOT change

- **Examples 1-3** in workflow.md — they demonstrate simpler patterns and don't need review loop context patterns
- **API reference, node types, model selection guide** — unchanged
- **"Important rules" section** — unchanged
- **Engine code (engine.py)** — no changes
- **dev-loop.py** — no changes, it's the source of truth we're aligning toward

## Risks

- **Example 4 length increases** from ~60 lines to ~120 lines. Acceptable because it's the canonical full-pipeline example, and the added complexity is real (not padding).
- **Integration test becomes more complex** with more nodes to verify. Mitigated by the test already having the infrastructure for multi-node workflows.
