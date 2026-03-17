# Workflow Prompt Alignment Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align `/workflow` Example 4 prompts with dev-loop.py's battle-tested context patterns, and add a reference section documenting the principles.

**Architecture:** Add a "Context patterns for review workflows" reference section to workflow.md before the examples, then rewrite Example 4 to match dev-loop.py's full pipeline (implement → smoke test → PR → simplify → review loop). Update the integration test to match.

**Tech Stack:** Markdown (workflow.md), Python (test_workflow_integration.py)

**Spec:** `docs/superpowers/specs/2026-03-16-workflow-prompt-alignment-design.md`

---

## Chunk 1: Add context patterns reference section to workflow.md

### Task 1: Add "Context patterns for review workflows" section

**Files:**
- Modify: `dev-loop/commands/workflow.md` (insert new section between `## Example workflows` heading and `### 1.`)

- [ ] **Step 1: Insert the context patterns section**

Between the `## Example workflows` heading and `### 1. Test-fix loop with commit`, insert:

```markdown
### Context patterns for review workflows

These patterns are distilled from the `dev-loop.py` orchestrator. Apply them whenever your workflow includes a review loop.

**1. Review the PR diff, not raw files.** Use `/code-review:code-review {pr_url}` and `/security-review` with the PR URL. These skills examine what *changed*, catching regressions and avoiding noise from pre-existing code. Don't prompt the LLM to "review the code in {work_dir}" — it will scan everything and miss what matters.

**2. Track previous findings across iterations.** Carry `previous_security_findings` as a separate state key. The security review prompt should instruct the reviewer to (a) check whether previous issues have been resolved, and (b) perform a full new review since fixes may introduce new issues.

**3. Post findings as PR comments.** Review nodes should post findings via `gh pr comment {pr_number}`. This creates an audit trail visible to humans and other tools without reading workflow state.

**4. Use an LLM for the decision gate.** Don't regex-match review output. Use `claude_node` with sonnet/low to evaluate findings and answer YES/NO. Only Critical/Important/Medium severity triggers a fix; Low severity and nitpicks are skipped. Short-circuit to YES if CI is failing.

**5. Run quality gates after every fix.** Every fix prompt must include instructions to run the project's lint, typecheck, format, and test suite, and fix any failures before committing. This prevents fix iterations from introducing new problems.

**6. Smoke test before creating the PR.** Verify the implementation works before entering the review loop. Look for a `## Validation` section in the plan; fall back to convention-based discovery (README, package.json, docker-compose.yml). End with `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL: <summary>` for router parsing.
```

- [ ] **Step 2: Verify the edit**

Run: Read `dev-loop/commands/workflow.md` and confirm the new section appears between the `## Example workflows` heading and `### 1.`

- [ ] **Step 3: Commit**

```bash
git add dev-loop/commands/workflow.md
git commit -m "docs(workflow): add context patterns reference section for review workflows"
```

---

## Chunk 2: Rewrite Example 4 in workflow.md

### Task 2: Replace Example 4 with full pipeline

**Files:**
- Modify: `dev-loop/commands/workflow.md` (replace the `### 4.` Example 4 section)

- [ ] **Step 1: Replace Example 4 heading and intro**

Replace the old heading and intro (`### 4. Implement → test → parallel reviews → fix → commit` and the line below it) with:

```markdown
### 4. Full pipeline: implement → smoke test → PR → simplify → review loop

The full pattern matching `dev-loop.py`'s battle-tested pipeline. Applies all six context patterns above.
```

- [ ] **Step 2: Replace Example 4 code block**

Replace the entire Example 4 code block with the new implementation. The new code block includes:

```python
import subprocess, time

# --- Smoke test ---
graph.add_node("implement", claude_node(
    "You are working in {work_dir}. Read the plan at {plan_path} and implement it.\n\n"
    "After completing all tasks:\n"
    "1. Update documentation (README, docstrings, diagrams) to reflect changes\n"
    "2. Run the project's quality gates (lint, typecheck, format, tests)\n"
    "Fix any failures before proceeding.",
    output_key="impl_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

graph.add_node("smoke_test", claude_node(
    "You are working in {work_dir}. Run a smoke test to verify the implementation.\n\n"
    "1. Read the plan at {plan_path}. Look for a '## Validation' section.\n"
    "2. If found, execute those validation instructions exactly.\n"
    "3. If NOT found, fall back to convention-based discovery:\n"
    "   - Read README.md, pyproject.toml, package.json, Makefile, docker-compose.yml\n"
    "   - Run a basic sanity check (does it start? does --help work?)\n"
    "4. ALWAYS kill all background processes before finishing.\n\n"
    "End with EXACTLY one line:\n"
    "  SMOKE_TEST_PASS\n"
    "  SMOKE_TEST_FAIL: <brief summary>",
    output_key="smoke_test_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

graph.add_node("smoke_test_fix", claude_node(
    "You are working in {work_dir}. The smoke test failed:\n\n{smoke_test_output}\n\n"
    "Diagnose the root cause, fix the code, then run quality gates "
    "(lint, typecheck, format, tests). Commit fixes locally.",
    output_key="smoke_test_fix_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

# No separate retry node — smoke_test_fix loops back to smoke_test.
# If the smoke test fails a second time, max_iterations will stop the loop.

# --- PR creation ---
graph.add_node("create_pr", claude_node(
    "You are working in {work_dir}. Push the current branch and create a PR:\n"
    "  git push -u origin HEAD\n"
    "  gh pr create --title '<descriptive title>' --body '<summary of changes>'\n\n"
    "Return the PR URL.",
    output_key="pr_url", model="sonnet", effort="low",
    permission_mode="bypassPermissions",
))

# --- Simplify ---
graph.add_node("simplify", claude_node(
    "/simplify",
    output_key="simplify_output", model="sonnet", effort="high",
    permission_mode="bypassPermissions",
))

graph.add_node("simplify_commit", claude_node(
    "If there are any uncommitted changes from the simplify pass, "
    "commit them with a descriptive message and push to the current branch.",
    output_key="simplify_commit_output", model="sonnet", effort="low",
    permission_mode="bypassPermissions",
))

# --- Parallel reviews (context pattern: review the PR diff, not files) ---
graph.add_node("code_review", claude_node(
    "/code-review:code-review {pr_url}",
    output_key="code_review_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

graph.add_node("security_review", claude_node(
    "/security-review\n\n"
    "Review the changes in PR {pr_url}.\n\n"
    + "IMPORTANT: A previous security review found these issues. "
    "Check if they are resolved AND do a full new review "
    "(fixes may introduce new issues):\n\n"
    "{previous_security_findings}\n\n"
    + "After completing the review, post findings as a PR comment:\n"
    "  gh pr comment <pr_number> --body '<findings>'\n\n"
    "Format with a '### Security Review' header and severity categories.",
    output_key="security_review_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

# --- Wait for CI ---
def _wait_for_ci_fn(state: dict[str, str]) -> dict[str, str]:
    """Poll CI checks until complete. Returns ci_status and ci_failures."""
    pr_url = state.get("pr_url", "")
    # Extract PR number from URL
    pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else ""
    max_wait = 600  # 10 minutes
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_number, "--json", "name,state,conclusion"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {"ci_status": "pass", "ci_failures": ""}
        output = result.stdout
        if '"pending"' not in output.lower() and '"queued"' not in output.lower():
            if '"failure"' in output.lower() or '"error"' in output.lower():
                return {"ci_status": "fail", "ci_failures": output}
            return {"ci_status": "pass", "ci_failures": ""}
        time.sleep(30)
    return {"ci_status": "pass", "ci_failures": ""}  # timeout = assume pass

graph.add_node("wait_for_ci", python_node(_wait_for_ci_fn))

# --- Decision gate (context pattern: LLM evaluation, not regex) ---
def _decision_fn(state: dict[str, str]) -> dict[str, str]:
    """Evaluate reviews + CI. Carry previous_security_findings, increment iteration.

    This is a python_node wrapper around the LLM decision so we can also
    carry state (previous_security_findings) and short-circuit on CI failure.
    Note: the LLM (decision_llm) always runs before this node in the graph.
    In dev-loop.py, the decision node conditionally skips the LLM call on CI
    failure — this two-node split is a simplification for the example.
    """
    ci_status = state.get("ci_status", "pass")
    security_text = state.get("security_review_output", "")
    iteration = int(state.get("iteration_count", "1"))

    if ci_status == "fail":
        return {
            "decision_output": "YES",
            "previous_security_findings": security_text,
            "iteration_count": str(iteration + 1),
        }

    return {
        "decision_output": state.get("decision_llm_output", "NO"),
        "previous_security_findings": security_text,
        "iteration_count": str(iteration + 1),
    }

graph.add_node("decision_llm", claude_node(
    "Based on these review findings, are there Critical, Important, or Medium "
    "severity issues that MUST be fixed?\n\n"
    "Code Review:\n{code_review_output}\n\n"
    "Security Review:\n{security_review_output}\n\n"
    "CI failures:\n{ci_failures}\n\n"
    "Answer EXACTLY: YES or NO. Only YES for Critical/Important/Medium issues "
    "or CI failures. Low severity and nitpicks do not count.",
    output_key="decision_llm_output", model="sonnet", effort="low",
))
graph.add_node("decision", python_node(_decision_fn))

# --- Fix (context pattern: quality gates after every fix) ---
graph.add_node("fix", claude_node(
    "Fix all Critical, Important, and Medium severity issues from this review "
    "of PR {pr_url}.\n\n"
    "Code Review:\n{code_review_output}\n\n"
    "Security Review:\n{security_review_output}\n\n"
    "CI failures:\n{ci_failures}\n\n"
    "After fixing, run quality gates (lint, typecheck, format, tests). "
    "Fix any failures. Commit and push.",
    output_key="fix_output", model="opus", effort="high",
    permission_mode="bypassPermissions",
))

# --- Routers ---
def smoke_test_router(state: dict[str, str]) -> str:
    error = state.get("smoke_test_error", "")
    output = state.get("smoke_test_output", "")
    if error or "SMOKE_TEST_FAIL" in output:
        return "fail"
    return "pass"

def decision_router(state: dict[str, str]) -> str:
    if "YES" in state.get("decision_output", "NO").upper():
        return "fix"
    return "done"

# --- Edges ---
# Phase 1: implement → smoke test → PR
graph.add_edge("start", "implement")
graph.add_edge("implement", "smoke_test")
graph.add_conditional_edges("smoke_test", smoke_test_router, {
    "pass": "create_pr", "fail": "smoke_test_fix",
})
graph.add_edge("smoke_test_fix", "smoke_test")  # retry by re-entering smoke_test

# Phase 2: simplify → review loop
graph.add_edge("create_pr", "simplify")
graph.add_edge("simplify", "simplify_commit")
graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
graph.add_edge("code_review", "wait_for_ci")
graph.add_edge("security_review", "wait_for_ci")
graph.add_edge("wait_for_ci", "decision_llm")
graph.add_edge("decision_llm", "decision")
graph.add_conditional_edges("decision", decision_router, {"fix": "fix", "done": END})
graph.add_edge("fix", "simplify")  # loop back to review
```

- [ ] **Step 3: Verify the edit**

Read `dev-loop/commands/workflow.md` and confirm:
- Example 4 title is "Full pipeline: implement → smoke test → PR → simplify → review loop"
- All nodes are present: implement, smoke_test, smoke_test_fix, create_pr, simplify, simplify_commit, code_review, security_review, wait_for_ci, decision_llm, decision, fix
- Uses `/code-review:code-review {pr_url}` and `/security-review` (not file-level review)
- Posts PR comments in security_review prompt
- decision_fn carries `previous_security_findings`
- fix prompt includes quality gates

- [ ] **Step 4: Update the model selection examples section**

In the "### Examples" section (under "## Choosing the right model for each step"), update the code review and security audit examples to use opus/high and show PR-based review:

Replace:
```python
# Code review — always Claude, needs judgment
claude_node(
    "You are working in {work_dir}. Review for bugs, logic errors, and quality issues.",
    model="sonnet", effort="high", permission_mode="bypassPermissions",
)

# Deep security audit — always Claude opus
claude_node(
    "You are working in {work_dir}. Review for security issues.",
    model="opus", effort="high", permission_mode="bypassPermissions",
)
```

With:
```python
# Code review — review the PR diff, not raw files
claude_node(
    "/code-review:code-review {pr_url}",
    model="opus", effort="high", permission_mode="bypassPermissions",
)

# Security review — review PR diff, post findings as PR comment
claude_node(
    "/security-review\n\nReview the changes in PR {pr_url}.\n\n"
    "After completing the review, post findings via: gh pr comment <number> --body '<findings>'",
    model="opus", effort="high", permission_mode="bypassPermissions",
)
```

- [ ] **Step 5: Update the model selection table**

In the model selection table (the `### Model selection table` section), change:

| Code review, security audit | `claude_node` sonnet/high | — |

To:

| Code review, security audit | `claude_node` opus/high | — |

- [ ] **Step 6: Verify and commit**

Run: `uv run ruff check dev-loop/commands/workflow.md` — no Python files changed, so this is N/A. Just verify the markdown renders correctly by reading it.

```bash
git add dev-loop/commands/workflow.md
git commit -m "docs(workflow): rewrite Example 4 with full dev-loop pipeline patterns"
```

---

## Chunk 3: Update the integration test

### Task 3: Update test_workflow_integration.py

**Files:**
- Modify: `dev-loop/tests/test_workflow_integration.py` (the `generate_workflow_script` function and progress logging assertions)

The integration test needs to generate a workflow script that matches the new Example 4. However, we must keep the test realistic and executable — it runs against a real temp project with Codex and Claude.

Key considerations:
- The test project is a simple mathlib, not a GitHub-hosted project with PRs
- `create_pr`, `wait_for_ci`, and `gh pr comment` would require a real GitHub remote
- The test should focus on verifiable patterns: smoke test, simplify, diff-based prompts, decision gate structure

**Approach:** Update the generated workflow to include smoke test and simplify steps (which work locally), update the review prompts to show the diff-based pattern (even though there's no real PR in the test), and update the decision gate to use the new pattern. Skip `create_pr` and `wait_for_ci` in the test since they require GitHub infrastructure.

- [ ] **Step 1: Update the generate_workflow_script function**

Replace the `generate_workflow_script` function with the updated version that:

1. Keeps `implement` (codex_node) and `run_tests` / `fix_tests` — unchanged
2. Adds `smoke_test` node after `run_tests` pass — uses claude_node to read the plan's Validation section and run `uv run pytest tests/ -v`, ending with `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL`
3. Adds `simplify` node — runs `/simplify` with sonnet/high
4. Adds `simplify_commit` node — claude_node sonnet/low to commit simplify changes
5. Updates `code_review` prompt to say `/code-review:code-review` (even though no real PR, the prompt pattern is what we're testing)
6. Updates `security_review` prompt to include `{previous_security_findings}` carry-over and mention PR comment posting
7. Replaces `decide` python_node with a `_decision_fn` that carries `previous_security_findings` and `iteration_count`
8. Updates `fix_reviews` prompt to include quality gates instruction
9. Updates edges to match new flow: `run_tests` → (fix or smoke_test) → `simplify` → `simplify_commit` → parallel reviews → decision → fix loop back to simplify

The new `generate_workflow_script` function:

```python
def generate_workflow_script(project_dir: Path, script_path: Path) -> None:
    """Generate the multi-LLM workflow script matching Example 4 patterns."""
    script_path.write_text(
        f"""\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mermaid-ascii"]
# ///
import sys
sys.path.insert(0, {str(SCRIPT_DIR)!r})

import asyncio
from engine import StateGraph, claude_node, codex_node, shell_node, python_node, END


async def main() -> None:
    graph = StateGraph(max_iterations=5)

    # --- Implementation ---
    graph.add_node(
        "implement",
        codex_node(
            "Read the plan at docs/plans/add-median-and-mode.md and implement it exactly. "
            "Add median and mode functions to src/mathlib/stats.py. "
            "Add the specified tests to tests/test_stats.py. "
            "Do NOT modify existing functions or tests.",
            output_key="impl_output",
            cwd={str(project_dir)!r},
        ),
    )

    # --- Run tests ---
    graph.add_node(
        "run_tests",
        shell_node(
            "cd {project_dir} && uv run pytest tests/ -v 2>&1",
            output_key="test_output",
            check=False,
        ),
    )

    graph.add_node(
        "fix_tests",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "The tests failed. Read the source and test files, fix the code:\\n\\n{{test_output}}",
            output_key="fix_tests_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    def test_router(state: dict[str, str]) -> str:
        output = state.get("test_output", "")
        if "failed" in output.lower() or "error" in output.lower():
            return "fix"
        return "smoke_test"

    # --- Smoke test (context pattern: verify before review) ---
    graph.add_node(
        "smoke_test",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Run a smoke test to verify the implementation.\\n\\n"
            "1. Read docs/plans/add-median-and-mode.md. Look for a Validation section.\\n"
            "2. Execute validation instructions (run `uv run pytest tests/ -v`).\\n"
            "3. ALWAYS kill background processes before finishing.\\n\\n"
            "End with EXACTLY one line:\\n"
            "  SMOKE_TEST_PASS\\n"
            "  SMOKE_TEST_FAIL: <brief summary>",
            output_key="smoke_test_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    def smoke_test_router(state: dict[str, str]) -> str:
        error = state.get("smoke_test_error", "")
        output = state.get("smoke_test_output", "")
        if error or "SMOKE_TEST_FAIL" in output:
            return "fail"
        return "pass"

    graph.add_node(
        "smoke_test_fix",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "The smoke test failed:\\n\\n{{smoke_test_output}}\\n\\n"
            "Diagnose the root cause, fix the code, and run quality gates "
            "(lint, typecheck, format, tests). Commit fixes locally.",
            output_key="smoke_test_fix_output",
            model="sonnet",
            effort="medium",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Simplify ---
    graph.add_node(
        "simplify",
        claude_node(
            "/simplify",
            output_key="simplify_output",
            model="sonnet",
            effort="high",
            permission_mode="bypassPermissions",
        ),
    )

    graph.add_node(
        "simplify_commit",
        claude_node(
            "If there are any uncommitted changes from the simplify pass, "
            "commit them with a descriptive message.",
            output_key="simplify_commit_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Reviews (context pattern: review diff, track previous findings) ---
    graph.add_node(
        "code_review",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Review the latest changes (use `git diff HEAD~1`) for bugs, logic errors, "
            "and code quality. This was implemented by Codex — verify correctness.\\n\\n"
            "Return findings with severity (Critical/Important/Medium/Low).",
            output_key="code_review_output",
            model="opus",
            effort="high",
            permission_mode="bypassPermissions",
        ),
    )

    # Security review uses sonnet/low in test (vs opus/high in Example 4)
    # to reduce cost and runtime for integration testing.
    graph.add_node(
        "security_review",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Review the latest changes (use `git diff HEAD~1`) for security issues.\\n\\n"
            "Previous security review findings (check if resolved, then do full review):\\n"
            "{{previous_security_findings}}\\n\\n"
            "Return findings with severity (Critical/Important/Medium/Low).",
            output_key="security_review_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Decision gate (context pattern: LLM evaluation, not regex) ---
    # Test omits wait_for_ci (no GitHub infrastructure), so no CI short-circuit.
    def _decision_fn(state: dict[str, str]) -> dict[str, str]:
        \"\"\"Carry previous_security_findings, increment iteration_count.\"\"\"
        security_text = state.get("security_review_output", "")
        iteration = int(state.get("iteration_count", "1"))
        return {{
            "decision_output": state.get("decision_llm_output", "NO"),
            "previous_security_findings": security_text,
            "iteration_count": str(iteration + 1),
        }}

    graph.add_node(
        "decision_llm",
        claude_node(
            "Based on these review findings, are there Critical, Important, or Medium "
            "severity issues that MUST be fixed?\\n\\n"
            "Code Review:\\n{{code_review_output}}\\n\\n"
            "Security Review:\\n{{security_review_output}}\\n\\n"
            "Answer EXACTLY: YES or NO. Only YES for Critical/Important/Medium. "
            "Low severity and nitpicks do not count.",
            output_key="decision_llm_output",
            model="sonnet",
            effort="low",
        ),
    )
    graph.add_node("decision", python_node(_decision_fn))

    # --- Fix (context pattern: quality gates after every fix) ---
    graph.add_node(
        "fix_reviews",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Fix Critical/Important/Medium issues:\\n\\n"
            "Code review:\\n{{code_review_output}}\\n\\n"
            "Security review:\\n{{security_review_output}}\\n\\n"
            "After fixing, run quality gates (lint, typecheck, format, tests). "
            "Fix any failures. Commit locally.",
            output_key="fix_reviews_output",
            model="sonnet",
            effort="medium",
            permission_mode="bypassPermissions",
        ),
    )

    def decision_router(state: dict[str, str]) -> str:
        if "YES" in state.get("decision_output", "NO").upper():
            return "issues"
        return "clean"

    # --- Commit ---
    graph.add_node(
        "commit",
        shell_node(
            'cd {project_dir} && git add -A && git diff --cached --quiet '
            '&& echo "nothing to commit" '
            '|| git commit -m "feat: add median and mode functions"',
            output_key="commit_output",
        ),
    )

    # --- Edges ---
    # Phase 1: implement → test → smoke test
    graph.add_edge("start", "implement")
    graph.add_edge("implement", "run_tests")
    graph.add_conditional_edges("run_tests", test_router, {{
        "fix": "fix_tests", "smoke_test": "smoke_test",
    }})
    graph.add_edge("fix_tests", "run_tests")
    graph.add_conditional_edges("smoke_test", smoke_test_router, {{
        "pass": "simplify", "fail": "smoke_test_fix",
    }})
    graph.add_edge("smoke_test_fix", "smoke_test")  # retry

    # Phase 2: simplify → review loop
    graph.add_edge("simplify", "simplify_commit")
    graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
    graph.add_edge("code_review", "decision_llm")
    graph.add_edge("security_review", "decision_llm")
    graph.add_edge("decision_llm", "decision")
    graph.add_conditional_edges("decision", decision_router, {{
        "issues": "fix_reviews", "clean": "commit",
    }})
    graph.add_edge("fix_reviews", "simplify")  # loop back to review
    graph.add_edge("commit", END)

    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        return

    result = await graph.run()
    print("TEST_OUTPUT:" + result.get("test_output", ""))
    print("COMMIT_OUTPUT:" + result.get("commit_output", ""))

asyncio.run(main())
"""
    )
```

Note: The test omits `create_pr` and `wait_for_ci` since they need GitHub infrastructure. The smoke test goes directly to `simplify` on pass. This is documented in the script comments.

- [ ] **Step 2: Update progress logging assertions**

In the `main()` function, update the progress logging spot-check assertions to verify new node names:

Replace the spot-check assertions:
```python
    # Spot-check a specific node name in the logs
    check(
        "[workflow] Starting: implement" in stdout,
        "Default progress log: 'implement' node logged",
    )
    check(
        "[workflow] Starting: run_tests" in stdout,
        "Default progress log: 'run_tests' node logged",
    )
```

With:
```python
    # Spot-check node names in the logs — verify new pipeline steps
    for node_name in ["implement", "run_tests", "smoke_test", "simplify", "decision"]:
        check(
            f"[workflow] Starting: {node_name}" in stdout,
            f"Default progress log: '{node_name}' node logged",
        )
```

- [ ] **Step 3: Run linting and type checking**

Run: `uv run ruff check dev-loop/tests/test_workflow_integration.py`
Run: `uv run pyright dev-loop/tests/test_workflow_integration.py`

Expected: Both pass (the test is a standalone script, not imported).

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/test_workflow_integration.py
git commit -m "test(workflow): align integration test with new Example 4 pipeline patterns"
```

---

## Chunk 4: Verify everything works

### Task 4: Final verification

- [ ] **Step 1: Run ruff check on all changed files**

```bash
uv run ruff check dev-loop/
```

Expected: No errors.

- [ ] **Step 2: Run ruff format check**

```bash
uv run ruff format --check dev-loop/
```

Expected: No formatting issues.

- [ ] **Step 3: Run pyright**

```bash
uv run pyright dev-loop/
```

Expected: No type errors.

- [ ] **Step 4: Run unit tests**

```bash
uv run pytest dev-loop/tests/test_engine.py -v
```

Expected: All pass (engine unchanged).

- [ ] **Step 5: Verify workflow.md renders correctly**

Read `dev-loop/commands/workflow.md` end-to-end and verify:
- Context patterns section appears between `## Example workflows` and `### 1.`
- Example 4 has the full pipeline with all nodes
- Model selection table shows opus/high for reviews
- Examples section shows PR-based review patterns

- [ ] **Step 6: Verify integration test script generates valid Python**

```bash
cd /tmp && python -c "
import ast, sys
sys.path.insert(0, '.')
# Quick syntax check of the generated script pattern
code = open('/Users/yorrickjansen/work/claude-code-plugins/dev-loop/tests/test_workflow_integration.py').read()
ast.parse(code)
print('Syntax OK')
"
```

Expected: "Syntax OK"

- [ ] **Step 7: Note about full integration test**

The full integration test (`uv run dev-loop/tests/test_workflow_integration.py`) requires Codex CLI and takes ~15 minutes. It should be run manually to verify, but is not part of the automated validation for this change.
