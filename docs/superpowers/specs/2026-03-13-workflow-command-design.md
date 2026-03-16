# Workflow Command Design

**Date:** 2026-03-13
**Status:** Approved

## Summary

Add a `/workflow` command to the dev-loop plugin that lets an agent generate and execute workflow scripts on the fly using the `StateGraph` engine API. When a user says "iterate until the tests pass," the agent writes a ~25-line Python script that defines the loop, writes it to disk, and runs it.

## Goals

- Make it trivial for an agent to create ad-hoc workflows from natural language
- Support Claude, Codex, and Gemini CLIs as workflow steps
- Support arbitrary shell commands as workflow steps
- Keep the engine as a single file in the plugin — no packaging

## Non-goals

- No installable package / PyPI publishing
- No declarative YAML/JSON format
- No built-in retries (agent builds control flow in the graph)
- No MCP node type
- No change to state type (`dict[str, str]`)

## Design

### 1. New `shell_node()` in `engine.py`

```python
def shell_node(
    command_template: str,
    output_key: str = "output",
) -> NodeFn:
```

- Interpolates state into `command_template` using `{key}` syntax (same as LLM nodes)
- Runs via `asyncio.create_subprocess_exec` through a shell
- Captures stdout as raw text into `output_key`
- Raises `RuntimeError` on non-zero exit code, with stderr in the error message

No other changes to the engine API.

### 2. Agent-generated scripts

The agent writes a Python script to `/tmp/workflow_{uuid}.py` and runs it with `uv run`.

Example generated script:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
import sys
sys.path.insert(0, "/path/to/dev-loop/scripts")

import asyncio
from engine import StateGraph, claude_node, shell_node, END

async def main():
    graph = StateGraph(max_iterations=5)

    graph.add_node("run_tests", shell_node("uv run pytest", output_key="test_output"))
    graph.add_node("fix", claude_node("Fix these test failures:\n{test_output}", output_key="fix_output"))

    async def router(state: dict[str, str]) -> str:
        return "done" if "passed" in state["test_output"].lower() else "fix"

    graph.add_edge("start", "run_tests")
    graph.add_conditional_edges("run_tests", router, {"fix": "fix", "done": END})
    graph.add_edge("fix", "run_tests")

    result = await graph.run()
    print(result["test_output"])

asyncio.run(main())
```

### 3. `/workflow` command

A new command in the dev-loop plugin. The command prompt contains:

- The absolute engine path (via `CLAUDE_PLUGIN_ROOT`)
- A compact API reference — node types, signatures, key patterns
- 2-3 example workflows the agent can adapt
- Instructions to write the script and run it

**Allowed tools:**
- `Bash` (to run `uv run /tmp/workflow_*.py`)
- `Write` (to create the script file)
- `Read`, `Glob`, `Grep` (to understand codebase context)

**Example interactions:**
- "Iterate until the tests pass" → test-fix loop
- "Lint, typecheck, and test in parallel" → parallel shell nodes
- "Have Gemini write the code, then Claude review it" → multi-LLM pipeline

### 4. Discovery

The plugin command tells the agent the engine path via `CLAUDE_PLUGIN_ROOT`. The generated script uses `sys.path.insert(0, ...)` to import the engine. No env vars, no copying.

### 5. Error handling

Fail fast. Nodes raise on failure. The agent builds retry/fix logic as graph edges. `max_iterations` prevents infinite loops.

## Testing

### Unit tests for `shell_node()`

- Successful command — captures stdout in correct output key
- Failed command (non-zero exit) — raises `RuntimeError` with stderr
- Template interpolation — `{key}` in command gets replaced from state
- Empty stdout — stores empty string

### Integration test

- Build a real graph with `shell_node` + `python_node`, run it, verify state flows correctly
- No mocked subprocess — runs actual `echo` / `true` / `false` commands

### Manual validation

- Run `/workflow` with "run pytest and fix until tests pass" against a test project
- Verify agent generates valid script and executes it
