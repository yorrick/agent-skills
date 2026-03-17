---
description: "Generate and run an ad-hoc workflow from a natural language description using the StateGraph engine"
argument-hint: "<what the workflow should do>"
allowed-tools: ["Bash(uv run /tmp/workflow_*)", "Bash(uv run pytest*)", "Bash(uv run ruff*)", "Write", "Read", "Glob", "Grep"]
---

# Workflow Generator

You generate and execute ad-hoc workflow scripts using the StateGraph engine.

The user's request is: $ARGUMENTS

## Engine location

The workflow engine is at: `${CLAUDE_PLUGIN_ROOT}/scripts/engine.py`

## What to do

1. **Understand the request.** Read relevant files if needed to understand the codebase context.
2. **Design the workflow.** Decide which nodes, edges, and routers are needed. Pick the right node type for each step. **Look for parallelization opportunities** — tasks that touch different files can run concurrently via `add_parallel_edges`. See the parallelization rules below.
3. **Write the script.** Create a Python script at `/tmp/workflow_NNNN.py` (use a random 4-digit suffix). Always include `--diagram` flag handling (see template).
4. **Show the diagram first.** Run with `uv run /tmp/workflow_NNNN.py --diagram` and show the user the rendered ASCII diagram so they can see the workflow graph before execution. The script template already uses `graph.to_ascii()` for this — do NOT change it to `to_mermaid()`. The ASCII version renders a visual box-and-arrow diagram directly in the terminal.
5. **Run it.** Execute with `uv run /tmp/workflow_NNNN.py`.
6. **Report the result.** Show the user what happened.

## Script template

Every generated script follows this structure:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mermaid-ascii"]
# ///
import os
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")

import asyncio
from engine import (
    StateGraph, claude_node, codex_node, gemini_node,
    shell_node, python_node, template_node,
    detect_available_models, END,
)


def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    """Build the workflow graph. Accepts optional models dict for testing."""
    if models is None:
        models = detect_available_models()
    HAS_CODEX = models["codex"]
    HAS_GEMINI = models["gemini"]

    graph = StateGraph(max_iterations=5)

    # ... define nodes and edges ...
    # Use codex_node/gemini_node when available and appropriate,
    # fall back to claude_node otherwise. See model selection guide below.

    return graph


if __name__ == "__main__":
    graph = build_graph()
    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        sys.exit(0)
    initial_state = {"work_dir": os.getcwd()}
    asyncio.run(graph.run(initial_state))
```

## API reference

### StateGraph

```python
graph = StateGraph(max_iterations=5)           # max loop iterations before raising MaxIterationsExceeded
graph.add_node("name", node_fn)                # register a node
graph.add_edge("start", "first_node")          # entry point (always start from "start")
graph.add_edge("a", "b")                       # unconditional edge
graph.add_edge("a", END)                       # terminate after node "a"
graph.add_conditional_edges("a", router_fn, {"label1": "b", "label2": END})  # conditional routing
graph.add_parallel_edges("a", ["b", "c"])       # run b and c concurrently, then join
result = await graph.run(initial_state)         # execute, returns final state dict
print(graph.to_ascii())                          # render ASCII box-and-arrow diagram
print(graph.to_mermaid())                       # generate raw Mermaid flowchart string
```

State is `dict[str, str]` — all values are strings. Each node receives the full state and returns a dict of keys to merge.

### Node types

**`claude_node(prompt_template, output_key="output", model="opus", effort="high", permission_mode="default")`**
Run a headless Claude CLI session. Best for complex reasoning, code review, multi-file changes.

**`codex_node(prompt_template, output_key="output")`**
Run a headless Codex CLI session. Good for code generation tasks.

**`gemini_node(prompt_template, output_key="output")`**
Run a headless Gemini CLI session. Good for quick tasks, summaries, alternative perspectives.

**`shell_node(command_template, output_key="output", check=True)`**
Run an arbitrary shell command. Captures stdout as raw text. Raises RuntimeError on non-zero exit by default. Set `check=False` to capture output regardless of exit code — use this for commands where failure is an expected signal (e.g. tests in a fix loop).

**`python_node(fn)`**
Wrap a sync or async Python function as a node. The function takes `dict[str, str]` and returns `dict[str, str]`.

**`template_node(template, output_key="output")`**
Interpolate state keys into a template string. Useful for combining outputs.

All templates use `{key}` syntax for state interpolation (Python `str.format_map`).

### Router functions

A router is a sync function `(dict[str, str]) -> str` that returns a label. The label is looked up in the route_map to determine the next node.

```python
def router(state: dict[str, str]) -> str:
    if "PASS" in state["test_output"]:
        return "done"
    return "fix"
```

## Choosing the right model for each step

The script template calls `detect_available_models()` at the top, giving you `HAS_CODEX` and `HAS_GEMINI` booleans. Use them to pick the best available model for each step.

### Decision flow

For each AI node, ask:

1. **Is it code generation from a clear spec, test fixes, or boilerplate?** → Use `codex_node` if `HAS_CODEX`, otherwise `claude_node` sonnet/medium
2. **Is it a summary, report, or quick triage?** → Use `gemini_node` if `HAS_GEMINI`, otherwise `claude_node` sonnet/low
3. **Does it need codebase navigation, multi-file reasoning, or judgment?** → `claude_node` sonnet/medium
4. **Does it need deep reasoning (architecture, security, subtle bugs)?** → `claude_node` opus/high

### Model selection table

| Task type | If Codex/Gemini available | Fallback |
|-----------|--------------------------|----------|
| Implement feature from a clear plan | `codex_node` | `claude_node` sonnet/medium |
| Fix test failures (error output provided) | `codex_node` | `claude_node` sonnet/medium |
| Generate tests, boilerplate, scaffolding | `codex_node` | `claude_node` sonnet/medium |
| Summarize findings, format a report | `gemini_node` | `claude_node` sonnet/low |
| Quick triage / classify | `gemini_node` | `claude_node` sonnet/low |
| Multi-file refactor, architectural changes | `claude_node` sonnet/medium | — |
| Code review, security audit | `claude_node` opus/high | — |
| Complex reasoning, tricky edge cases | `claude_node` opus/high | — |
| Trivial fix (typo, rename) | `claude_node` sonnet/low | — |

### Claude effort levels

| Effort | When to use |
|--------|-------------|
| `effort="low"` | Trivial changes: typo fix, rename, simple one-liner, summaries |
| `effort="medium"` | Standard work: implement a function, fix a bug, refactor a file |
| `effort="high"` | Deep work: review for subtle issues, multi-file architecture, security audit |

### Examples

```python
# Implement from plan — Codex if available, else Claude
implement_node = (
    codex_node("Implement this feature in {work_dir}: {description}", output_key="code")
    if HAS_CODEX else
    claude_node(
        "You are working in {work_dir}. Implement this feature: {description}",
        model="sonnet", effort="medium", permission_mode="bypassPermissions",
    )
)

# Fix failing tests — Codex if available, else Claude
fix_node = (
    codex_node("Fix the failing tests in {work_dir}:\n\n{test_output}", output_key="fix_output")
    if HAS_CODEX else
    claude_node(
        "You are working in {work_dir}. Fix the failing tests:\n\n{test_output}",
        model="sonnet", effort="medium", permission_mode="bypassPermissions",
    )
)

# Summarize — Gemini if available, else Claude
summary_node = (
    gemini_node("Summarize these findings:\n\n{review_output}", output_key="summary")
    if HAS_GEMINI else
    claude_node(
        "Summarize these findings:\n\n{review_output}",
        model="sonnet", effort="low",
    )
)

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

## Example workflows

### Context patterns for review workflows

These patterns are distilled from the `dev-loop.py` orchestrator. Apply them whenever your workflow includes a review loop.

**1. Review the PR diff, not raw files.** Use `/code-review:code-review {pr_url}` and `/security-review` with the PR URL. These skills examine what *changed*, catching regressions and avoiding noise from pre-existing code. Don't prompt the LLM to "review the code in {work_dir}" — it will scan everything and miss what matters.

**2. Track previous findings across iterations.** Carry `previous_security_findings` as a separate state key. The security review prompt should instruct the reviewer to (a) check whether previous issues have been resolved, and (b) perform a full new review since fixes may introduce new issues.

**3. Post findings as PR comments.** Review nodes should post findings via `gh pr comment {pr_number}`. This creates an audit trail visible to humans and other tools without reading workflow state.

**4. Use an LLM for the decision gate.** Don't regex-match review output. Use `claude_node` with sonnet/low to evaluate findings and answer YES/NO. Only Critical/Important/Medium severity triggers a fix; Low severity and nitpicks are skipped. Short-circuit to YES if CI is failing.

**5. Run quality gates after every fix.** Every fix prompt must include instructions to run the project's lint, typecheck, format, and test suite, and fix any failures before committing. This prevents fix iterations from introducing new problems.

**6. Smoke test before creating the PR.** Verify the implementation works before entering the review loop. Look for a `## Validation` section in the plan; fall back to convention-based discovery (README, package.json, docker-compose.yml). End with `SMOKE_TEST_PASS` or `SMOKE_TEST_FAIL: <summary>` for router parsing.

### 1. Test-fix loop with commit

```python
def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()

    graph = StateGraph(max_iterations=5)

    graph.add_node("test", shell_node("uv run pytest -x", output_key="test_output", check=False))
    graph.add_node("fix", claude_node(
        "These tests failed. Fix the code:\n\n{test_output}",
        output_key="fix_output",
        model="sonnet", effort="medium",
        permission_mode="bypassPermissions",
    ))
    graph.add_node("commit", shell_node(
        "git add -A && git commit -m 'fix: resolve test failures' && git push",
        output_key="commit_output",
    ))

    def test_router(state: dict[str, str]) -> str:
        return "done" if "passed" in state["test_output"].lower() else "fix"

    graph.add_edge("start", "test")
    graph.add_conditional_edges("test", test_router, {"fix": "fix", "done": "commit"})
    graph.add_edge("fix", "test")
    graph.add_edge("commit", END)
    return graph
```

### 2. Parallel lint + typecheck + test

```python
def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()

    graph = StateGraph(max_iterations=5)

    graph.add_node("setup", python_node(lambda s: {}))
    graph.add_node("lint", shell_node("uv run ruff check .", output_key="lint_output", check=False))
    graph.add_node("typecheck", shell_node("uv run pyright", output_key="typecheck_output", check=False))
    graph.add_node("test", shell_node("uv run pytest", output_key="test_output", check=False))
    graph.add_node("report", template_node(
        "Lint:\n{lint_output}\n\nTypecheck:\n{typecheck_output}\n\nTests:\n{test_output}",
        output_key="report",
    ))

    graph.add_edge("start", "setup")
    graph.add_parallel_edges("setup", ["lint", "typecheck", "test"])
    graph.add_edge("lint", "report")
    graph.add_edge("typecheck", "report")
    graph.add_edge("test", "report")
    graph.add_edge("report", END)
    return graph
```

### 3. Implement → review → commit

```python
def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()

    graph = StateGraph(max_iterations=5)

    graph.add_node("implement", claude_node(
        "You are working in {work_dir}. Implement this feature: {description}",
        output_key="code",
        model="sonnet", effort="medium",
        permission_mode="bypassPermissions",
    ))
    graph.add_node("review", claude_node(
        "You are working in {work_dir}. Review this implementation for bugs and improvements.",
        output_key="review",
        model="sonnet", effort="high",
        permission_mode="bypassPermissions",
    ))
    graph.add_node("commit", shell_node(
        "cd {work_dir} && git add -A && git commit -m 'feat: {description}' && git push",
        output_key="commit_output",
    ))

    graph.add_edge("start", "implement")
    graph.add_edge("implement", "review")
    graph.add_edge("review", "commit")
    graph.add_edge("commit", END)
    return graph
```

### 4. Full pipeline: implement → smoke test → PR → simplify → review loop

The full pattern matching `dev-loop.py`'s battle-tested pipeline. Applies all six context patterns above.

```python
import subprocess, time


# --- Helper functions (module-level, don't affect graph topology) ---

def _wait_for_ci_fn(state: dict[str, str]) -> dict[str, str]:
    """Poll CI checks until complete. Returns ci_status and ci_failures."""
    pr_url = state.get("pr_url", "")
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


def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()
    HAS_CODEX = models["codex"]
    HAS_GEMINI = models["gemini"]

    graph = StateGraph(max_iterations=5)

    # --- Implementation ---
    graph.add_node("implement", claude_node(
        "You are working in {work_dir}. Read the plan at {plan_path} and implement it.\n\n"
        "After completing all tasks:\n"
        "1. Update documentation (README, docstrings, diagrams) to reflect changes\n"
        "2. Run the project's quality gates (lint, typecheck, format, tests)\n"
        "Fix any failures before proceeding.",
        output_key="impl_output", model="opus", effort="high",
        permission_mode="bypassPermissions",
    ))

    # --- Smoke test (context pattern 6) ---
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

    # --- Parallel reviews (context patterns 1, 2, 3) ---
    graph.add_node("code_review", claude_node(
        "/code-review:code-review {pr_url}",
        output_key="code_review_output", model="opus", effort="high",
        permission_mode="bypassPermissions",
    ))

    graph.add_node("security_review", claude_node(
        "/security-review\n\n"
        "Review the changes in PR {pr_url}.\n\n"
        "IMPORTANT: A previous security review found these issues. "
        "Check if they are resolved AND do a full new review "
        "(fixes may introduce new issues):\n\n"
        "{previous_security_findings}\n\n"
        "After completing the review, post findings as a PR comment:\n"
        "  gh pr comment <pr_number> --body '<findings>'\n\n"
        "Format with a '### Security Review' header and severity categories.",
        output_key="security_review_output", model="opus", effort="high",
        permission_mode="bypassPermissions",
    ))

    # --- Wait for CI ---
    graph.add_node("wait_for_ci", python_node(_wait_for_ci_fn))

    # --- Decision gate (context pattern 4) ---
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

    # --- Fix (context pattern 5) ---
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

    return graph
```

### 5. Parallel plan tasks with deferred commit

When a plan has independent tasks that touch different files, run them in parallel. Each parallel node does its work but does NOT commit. A shared commit node after the join handles all changes.

```python
def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()

    graph = StateGraph(max_iterations=3)

    # Two independent tasks that touch different files
    graph.add_node("fan_out", python_node(lambda s: {}))
    graph.add_node("task_a", claude_node(
        "You are working in {work_dir}. Implement task A: {task_a_description}\n\n"
        "Do NOT commit — just make the changes and run tests.",
        output_key="task_a_output",
        model="sonnet", effort="medium",
        permission_mode="bypassPermissions",
    ))
    graph.add_node("task_b", claude_node(
        "You are working in {work_dir}. Implement task B: {task_b_description}\n\n"
        "Do NOT commit — just make the changes and run tests.",
        output_key="task_b_output",
        model="sonnet", effort="medium",
        permission_mode="bypassPermissions",
    ))
    # Shared commit after both tasks complete
    graph.add_node("commit_all", shell_node(
        'cd {work_dir} && git add -A && git diff --cached --quiet && echo "nothing to commit" '
        '|| git commit -m "feat: implement task A and task B"',
        output_key="commit_output",
    ))

    graph.add_edge("start", "fan_out")
    graph.add_parallel_edges("fan_out", ["task_a", "task_b"])
    graph.add_edge("task_a", "commit_all")
    graph.add_edge("task_b", "commit_all")
    graph.add_edge("commit_all", END)

    return graph
```

## Important rules

- **Use `build_graph()`.** Always put graph construction in a `build_graph(models=None)` function.
  The `if __name__` block calls it and runs the graph. This makes scripts importable for testing.
  `build_graph()` must accept an optional `models` dict (defaulting to `detect_available_models()`)
  so callers can control model availability.
- **Fail fast.** Don't add retries to nodes. If you need retry logic, build it as a loop in the graph (conditional edge back to a fix node).
- **State is strings.** All state values are strings. Use `python_node` to parse or transform if needed.
- **Set max_iterations.** Always set a reasonable `max_iterations` to prevent infinite loops. Default is 5.
- **Right-size the model.** Use `HAS_CODEX`/`HAS_GEMINI` from `detect_available_models()` to pick the best available tool for each step. Use Codex for code gen, Gemini for text tasks, and Claude when you need codebase navigation or deep reasoning. See the model selection guide above.
- **Permission mode.** For Claude nodes that need to edit files, set `permission_mode="bypassPermissions"` for headless execution.
- **Always commit at the end.** If the workflow modifies code, add a final `shell_node` that commits and pushes the changes.
- **Safe commits.** Use `git diff --cached --quiet && echo "nothing to commit" || git commit -m "..."` to handle cases where there's nothing to commit.
- **Working directory in prompts.** Always include `You are working in {work_dir}` in Claude node prompts so headless sessions know where to find files.
- **Parallel reviews need a fan-out node.** Use a passthrough `python_node(lambda s: {})` before `add_parallel_edges` since conditional and parallel edges on the same node conflict.
- **Parallelize independent work.** When executing a plan with multiple tasks that touch different files, run them concurrently with `add_parallel_edges`. Parallel work nodes must NOT commit — each node does its implementation only (edit files, run tests). Add a shared commit node after the parallel group that stages and commits all changes together. This avoids git race conditions while maximizing throughput. See example 5 below.
- **Identify parallelizable tasks.** Two tasks can run in parallel when: (a) they modify different files, (b) neither depends on the other's output, and (c) neither needs to read files the other will modify. When in doubt, keep tasks sequential — incorrect parallelization causes subtle bugs.
