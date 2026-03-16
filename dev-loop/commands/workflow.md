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
from engine import StateGraph, claude_node, codex_node, gemini_node, shell_node, python_node, template_node, END

async def main():
    graph = StateGraph(max_iterations=5)

    # ... define nodes and edges ...

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

Pick the cheapest/fastest option that can handle the step. Don't default to `claude_node` for everything — **Codex and Gemini are faster and cheaper** for many tasks. Use Claude only when you need its unique strengths (complex reasoning, multi-file refactoring, nuanced review).

### Decision flow

Ask yourself for each node:

1. **Is it a shell command?** → `shell_node` (tests, lint, git, etc.)
2. **Is it pure code generation from a clear spec/plan?** → `codex_node` (fastest for writing code)
3. **Is it a quick summary, reformatting, or simple text task?** → `gemini_node` (fast, good at text)
4. **Does it need to read many files, navigate a codebase, or make judgment calls?** → `claude_node`
5. **Does it need deep reasoning about architecture, subtle bugs, or security?** → `claude_node` with `model="opus", effort="high"`

### Model selection table

| Task type | Node type | Why |
|-----------|-----------|-----|
| Implement feature from a clear plan | `codex_node` | Codex excels at code gen from specs — faster than Claude for straightforward implementation |
| Fix test failures (error output provided) | `codex_node` | Mechanical fix with clear error context — Codex handles this well |
| Generate tests, boilerplate, scaffolding | `codex_node` | Routine code generation is Codex's sweet spot |
| Summarize findings, format a report | `gemini_node` | Text synthesis is fast with Gemini |
| Quick triage / classify (e.g., "are there issues?") | `gemini_node` | Gemini is fast for simple decision-making on text |
| Multi-file refactor, architectural changes | `claude_node` sonnet/medium | Needs codebase navigation and judgment |
| Code review, security audit | `claude_node` sonnet/high | Needs careful analysis across files |
| Complex debugging, tricky edge cases | `claude_node` opus/high | Needs deep reasoning |
| Trivial fix (typo, rename) | `claude_node` sonnet/low | Simple but needs file access |

### Claude effort levels (only applies to `claude_node`)

| Effort | When to use |
|--------|-------------|
| `effort="low"` | Trivial changes: typo fix, rename, simple one-liner |
| `effort="medium"` | Standard work: implement a function, fix a bug, refactor a file |
| `effort="high"` | Deep work: review for subtle issues, multi-file architecture, security audit |

### Examples

```python
# Implement from plan — use Codex, it's faster for code gen
codex_node("Implement this feature: {description}", output_key="code")

# Fix failing tests — Codex can handle mechanical fixes
codex_node(
    "Fix the failing tests. Here's the error output:\n\n{test_output}",
    output_key="fix_output",
)

# Generate test boilerplate — Codex is great at this
codex_node(
    "Write pytest unit tests for the functions in {file_path}:\n\n{code}",
    output_key="tests",
)

# Summarize review findings — Gemini is fast for text
gemini_node(
    "Summarize these review findings into a brief report:\n\n"
    "Code review:\n{code_review_output}\n\nSecurity:\n{security_review_output}",
    output_key="summary",
)

# Quick triage — Gemini for fast classification
gemini_node(
    "Are there any Critical or Important issues in this review? "
    "Reply 'yes' or 'no'.\n\n{review_output}",
    output_key="triage",
)

# Deep code review — Claude with high effort
claude_node(
    "Review for security issues: {code}",
    model="opus", effort="high",
)

# Multi-file refactor — Claude needed for navigation
claude_node(
    "You are working in {work_dir}. Refactor the auth module to use the new token format.",
    model="sonnet", effort="medium",
    permission_mode="bypassPermissions",
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

### 3. Multi-LLM pipeline — Codex implements, Claude reviews

Each model does what it's best at: Codex generates code fast, Claude reviews it carefully.

```python
graph.add_node("implement", codex_node(
    "Implement this feature: {description}",
    output_key="code",
))
graph.add_node("review", claude_node(
    "Review this implementation for bugs and improvements:\n\n{code}",
    output_key="review",
    model="sonnet", effort="high",
))
graph.add_node("commit", shell_node(
    "git add -A && git commit -m 'feat: {description}' && git push",
    output_key="commit_output",
))

graph.add_edge("start", "implement")
graph.add_edge("implement", "review")
graph.add_edge("review", "commit")
graph.add_edge("commit", END)
```

### 4. Implement → test → parallel reviews → fix → commit (multi-LLM)

The full pattern using the right model for each step — Codex for code gen/fixes, Claude for reviews:

```python
# Codex for implementation — fast code gen from a clear plan
graph.add_node("implement", codex_node(
    "Read the plan at {plan_path} and implement it in {work_dir}.",
    output_key="impl_output",
))
graph.add_node("run_tests", shell_node(
    "cd {work_dir} && uv run pytest -v 2>&1",
    output_key="test_output", check=False,
))
# Codex for test fixes — mechanical fix with clear error output
graph.add_node("fix_tests", codex_node(
    "Fix the failing tests in {work_dir}:\n\n{test_output}",
    output_key="fix_tests_output",
))
graph.add_node("start_reviews", python_node(lambda s: {}))
# Claude for reviews — needs judgment and careful analysis
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
# Codex for fixes — applying review feedback is mechanical
graph.add_node("fix_reviews", codex_node(
    "Fix Critical/Important/Medium issues in {work_dir}:\n\n"
    "Code review:\n{code_review_output}\n\nSecurity review:\n{security_review_output}",
    output_key="fix_reviews_output",
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
- **Right-size the model.** Default to `codex_node` for code generation and `gemini_node` for text tasks. Only use `claude_node` when you need codebase navigation, multi-file reasoning, or nuanced judgment. See the "Choosing the right model" section above.
- **Permission mode.** For Claude nodes that need to edit files, set `permission_mode="bypassPermissions"` for headless execution.
- **Always commit at the end.** If the workflow modifies code, add a final `shell_node` that commits and pushes the changes.
- **Safe commits.** Use `git diff --cached --quiet && echo "nothing to commit" || git commit -m "..."` to handle cases where there's nothing to commit.
- **Working directory in prompts.** Always include `You are working in {work_dir}` in Claude node prompts so headless sessions know where to find files.
- **Parallel reviews need a fan-out node.** Use a passthrough `python_node(lambda s: {})` before `add_parallel_edges` since conditional and parallel edges on the same node conflict.
