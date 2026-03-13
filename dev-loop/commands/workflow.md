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
4. **Show the diagram first.** Run with `uv run /tmp/workflow_NNNN.py --diagram` and show the user the Mermaid flowchart so they can see the workflow graph before execution.
5. **Run it.** Execute with `uv run /tmp/workflow_NNNN.py`.
6. **Report the result.** Show the user what happened.

## Script template

Every generated script follows this structure:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
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

## Example workflows

### 1. Test-fix loop

```python
graph.add_node("test", shell_node("uv run pytest -x", output_key="test_output", check=False))
graph.add_node("fix", claude_node(
    "These tests failed. Fix the code:\n\n{test_output}",
    output_key="fix_output",
    permission_mode="bypassPermissions",
))

async def test_router(state: dict[str, str]) -> str:
    return "done" if "passed" in state["test_output"].lower() else "fix"

graph.add_edge("start", "test")
graph.add_conditional_edges("test", test_router, {"fix": "fix", "done": END})
graph.add_edge("fix", "test")
```

### 2. Parallel lint + typecheck + test

```python
graph.add_node("setup", python_node(lambda s: {}))
graph.add_node("lint", shell_node("uv run ruff check .", output_key="lint_output"))
graph.add_node("typecheck", shell_node("uv run pyright", output_key="typecheck_output"))
graph.add_node("test", shell_node("uv run pytest", output_key="test_output"))
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

### 3. Multi-LLM pipeline

```python
graph.add_node("implement", codex_node(
    "Implement this feature: {description}",
    output_key="code",
))
graph.add_node("review", claude_node(
    "Review this implementation for bugs and improvements:\n\n{code}",
    output_key="review",
))

graph.add_edge("start", "implement")
graph.add_edge("implement", "review")
graph.add_edge("review", END)
```

## Important rules

- **Fail fast.** Don't add retries to nodes. If you need retry logic, build it as a loop in the graph (conditional edge back to a fix node).
- **State is strings.** All state values are strings. Use `python_node` to parse or transform if needed.
- **Set max_iterations.** Always set a reasonable `max_iterations` to prevent infinite loops. Default is 5.
- **Use the right LLM.** Claude for complex reasoning and code changes. Codex for code generation. Gemini for quick tasks. Shell for non-LLM commands.
- **Permission mode.** For Claude nodes that need to edit files, set `permission_mode="bypassPermissions"` to skip approval prompts.
