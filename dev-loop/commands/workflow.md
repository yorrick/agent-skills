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
2. **Design the workflow.** Decide which nodes, edges, and routers are needed. Pick the right node type for each step.
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
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")

import asyncio
from engine import (
    StateGraph, claude_node, codex_node, gemini_node,
    shell_node, python_node, template_node,
    detect_available_models, END,
)

# Detect which AI CLIs are installed
models = detect_available_models()
HAS_CODEX = models["codex"]
HAS_GEMINI = models["gemini"]

async def main():
    graph = StateGraph(max_iterations=5)

    # ... define nodes and edges ...
    # Use codex_node/gemini_node when available and appropriate,
    # fall back to claude_node otherwise. See model selection guide below.

    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        return

    result = await graph.run()
    print(result.get("output", ""))

asyncio.run(main())
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
| Code review, security audit | `claude_node` sonnet/high | — |
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

## Example workflows

### 1. Test-fix loop with commit

```python
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
```

### 2. Parallel lint + typecheck + test

```python
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
```

### 3. Implement → review → commit

```python
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
```

### 4. Implement → test → parallel reviews → fix → commit

The full pattern with code review and security review running in parallel:

```python
graph.add_node("implement", claude_node(
    "You are working in {work_dir}. Read the plan at {plan_path} and implement it.",
    output_key="impl_output", model="sonnet", effort="medium",
    permission_mode="bypassPermissions",
))
graph.add_node("run_tests", shell_node(
    "cd {work_dir} && uv run pytest -v 2>&1",
    output_key="test_output", check=False,
))
graph.add_node("fix_tests", claude_node(
    "You are working in {work_dir}. Fix the failing tests:\n\n{test_output}",
    output_key="fix_tests_output", model="sonnet", effort="low",
    permission_mode="bypassPermissions",
))
graph.add_node("start_reviews", python_node(lambda s: {}))
graph.add_node("code_review", claude_node(
    "You are working in {work_dir}. Review the code for bugs, logic errors, and quality issues. "
    "Return findings with severity (Critical/Important/Medium/Low).",
    output_key="code_review_output", model="sonnet", effort="high",
    permission_mode="bypassPermissions",
))
graph.add_node("security_review", claude_node(
    "You are working in {work_dir}. Review for security issues: injection, data exposure, "
    "unsafe operations. Return findings with severity.",
    output_key="security_review_output", model="sonnet", effort="high",
    permission_mode="bypassPermissions",
))
graph.add_node("decision", python_node(decide_fn))
graph.add_node("fix_reviews", claude_node(
    "You are working in {work_dir}. Fix Critical/Important/Medium issues:\n\n"
    "Code review:\n{code_review_output}\n\nSecurity review:\n{security_review_output}",
    output_key="fix_reviews_output", model="sonnet", effort="medium",
    permission_mode="bypassPermissions",
))
graph.add_node("commit", shell_node(
    'cd {work_dir} && git add -A && git diff --cached --quiet && echo "nothing to commit" '
    '|| git commit -m "feat: implement feature"',
    output_key="commit_output",
))

def test_router(state):
    return "fix" if "failed" in state["test_output"].lower() else "review"

graph.add_edge("start", "implement")
graph.add_edge("implement", "run_tests")
graph.add_conditional_edges("run_tests", test_router, {"fix": "fix_tests", "review": "start_reviews"})
graph.add_edge("fix_tests", "run_tests")
graph.add_parallel_edges("start_reviews", ["code_review", "security_review"])
graph.add_edge("code_review", "decision")
graph.add_edge("security_review", "decision")
graph.add_conditional_edges("decision", lambda s: s.get("decision", "clean"), {"issues": "fix_reviews", "clean": "commit"})
graph.add_edge("fix_reviews", "run_tests")
graph.add_edge("commit", END)
```

## Important rules

- **Fail fast.** Don't add retries to nodes. If you need retry logic, build it as a loop in the graph (conditional edge back to a fix node).
- **State is strings.** All state values are strings. Use `python_node` to parse or transform if needed.
- **Set max_iterations.** Always set a reasonable `max_iterations` to prevent infinite loops. Default is 5.
- **Right-size the model.** Use `HAS_CODEX`/`HAS_GEMINI` from `detect_available_models()` to pick the best available tool for each step. Use Codex for code gen, Gemini for text tasks, and Claude when you need codebase navigation or deep reasoning. See the model selection guide above.
- **Permission mode.** For Claude nodes that need to edit files, set `permission_mode="bypassPermissions"` for headless execution.
- **Always commit at the end.** If the workflow modifies code, add a final `shell_node` that commits and pushes the changes.
- **Safe commits.** Use `git diff --cached --quiet && echo "nothing to commit" || git commit -m "..."` to handle cases where there's nothing to commit.
- **Working directory in prompts.** Always include `You are working in {work_dir}` in Claude node prompts so headless sessions know where to find files.
- **Parallel reviews need a fan-out node.** Use a passthrough `python_node(lambda s: {})` before `add_parallel_edges` since conditional and parallel edges on the same node conflict.
