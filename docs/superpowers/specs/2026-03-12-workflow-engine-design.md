# Workflow Engine Design

## Overview

A lightweight async graph execution engine for orchestrating headless AI coding agents (Claude Code, Codex, Gemini CLI). The engine lives inside the dev-loop plugin and replaces the manual orchestration logic in `dev-loop.py`.

## Goals

- LangGraph-inspired Python API that LLMs can easily generate
- Async execution with parallel node support
- Cycles/loops with safety valves
- CLI-agnostic: nodes run any subprocess
- Minimal: ~300-400 lines, no dependencies beyond stdlib + pydantic (optional)

## Non-goals (for now)

- UI / visualization
- Checkpointing / resume from failure
- Pydantic-typed state (opt-in for later)
- Standalone package / PyPI publishing

## File layout

```
dev-loop/
  scripts/
    engine.py       # ~300-400 lines — the graph engine
    dev-loop.py     # refactored to use engine
```

## Engine API

### Graph builder

```python
from engine import StateGraph, END

graph = StateGraph(max_iterations=5)

graph.add_node("implement", claude_node("Implement the plan:\n{issue_body}"))
graph.add_node("review", claude_node("Review this code:\n{implementation_output}"))
graph.add_node("fix", claude_node("Fix these issues:\n{review_output}"))

graph.add_edge("start", "implement")
graph.add_edge("implement", "review")
graph.add_conditional_edges("review", should_continue, {"fix": "fix", "done": END})
graph.add_edge("fix", "review")  # loop back

result = await graph.run(initial_state={"issue_body": "the GitHub issue body"})
```

### Core types

- **`State`** — `dict[str, str]` — shared state dictionary. Each node reads named keys and writes named keys.
- **`NodeFn`** — async callable `(dict[str, str]) -> dict[str, str]`. Receives full state, returns keys to merge.
- **`Edge`** — unconditional: source -> target
- **`ConditionalEdge`** — source -> router function `(dict[str, str]) -> str` -> map of `{label: target_node}`
- **`StateGraph`** — builder + executor
- **`END`** — sentinel signaling graph completion

### State model

State is a `dict[str, str]` shared across all nodes. Each node receives the full state dict and returns a dict of keys to merge back (shallow merge). This allows nodes to access any previously-written value by name.

```python
# A node that reads pr_url and code_review_output, writes fix_output
async def fix_node(state: dict[str, str]) -> dict[str, str]:
    prompt = f"Fix issues in PR {state['pr_url']}:\n{state['code_review_output']}"
    result = await run_claude(prompt)
    return {"fix_output": result}
```

For parallel nodes joining into a single successor, each parallel node writes to its own key. The successor reads whichever keys it needs — no concatenation magic.

### Parallel execution

```python
graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
graph.add_edge("code_review", "wait_for_ci")
graph.add_edge("security_review", "wait_for_ci")
```

- `add_parallel_edges(source, targets)` — after source completes, all targets run concurrently via `asyncio.gather()`
- A node with multiple incoming edges waits for **all** predecessors to complete before running (join semantics)
- Each parallel node writes to its own state keys — no merge conflicts

### Node types

All nodes are async callables `(dict[str, str]) -> dict[str, str]`. Convenience constructors:

```python
# CLI subprocess nodes
claude_node(
    prompt_template: str,
    output_key: str = "output",
    model: str = "opus",
    effort: str = "high",
    permission_mode: str = "default",
) -> NodeFn

codex_node(prompt_template: str, output_key: str = "output") -> NodeFn
gemini_node(prompt_template: str, output_key: str = "output") -> NodeFn

# Python function node (sync or async)
python_node(fn: Callable[[dict[str, str]], dict[str, str]]) -> NodeFn

# String template node — reads keys from state, writes to output_key
template_node(template: str, output_key: str = "output") -> NodeFn
```

Prompt templates use `{key_name}` for interpolation from state:

```python
graph.add_node("review", claude_node(
    "Review the code at PR {pr_url}. Previous findings:\n{previous_findings}",
    output_key="code_review_output",
    model="opus",
))
```

### Router functions

For conditional edges, a router reads state and returns an edge label:

```python
def needs_fix(state: dict[str, str]) -> str:
    decision = state["decision_output"]
    if "no issues" in decision.lower():
        return "done"
    return "fix"
```

### Event callbacks

```python
@graph.on_node_start
async def on_start(node_name: str, state: dict[str, str]):
    ...

@graph.on_node_end
async def on_end(node_name: str, state: dict[str, str]):
    ...

@graph.on_error
async def on_error(node_name: str, error: Exception):
    ...
```

Three events only. Enough for logging, status, notifications, and a future UI.

### Error handling

- An unhandled exception in a node **stops the entire graph** and raises to the caller.
- The `on_error` callback is notified before the exception propagates, but **cannot suppress or recover** from errors.
- For CLI nodes: a subprocess returning a non-zero exit code or an `is_error: true` JSON field is treated as a node error.
- The caller (dev-loop.py) handles cleanup, PR comments, and exit codes.

### Iteration tracking

`max_iterations` counts how many times any **backward edge** (an edge pointing to a node that was already visited) is traversed. This counts loop iterations, not total edge traversals.

Example: with `max_iterations=5` and a loop `fix -> simplify`, the engine allows 5 full review cycles before stopping.

When the limit is reached, the engine raises a `MaxIterationsExceeded` exception. The caller decides how to handle it (e.g., post a PR comment and exit).

### Entry points

`graph.run()` accepts an optional `start_node` parameter to begin execution at any named node:

```python
# Default mode: full workflow
await graph.run(initial_state=state)

# --review-only mode: skip to review loop
await graph.run(initial_state=state, start_node="simplify")

# --continue-pr mode: skip worktree setup
await graph.run(initial_state=state, start_node="implement")
```

### Graph-level configuration

```python
graph = StateGraph(
    max_iterations=5,          # loop iteration limit
    cwd=worktree_path,         # working directory for all subprocess nodes
)
```

`cwd` is passed to all CLI subprocess nodes. Individual nodes can override it via `python_node` if needed.

## Dev-loop refactor

### What moves to engine.py

- Graph construction and validation
- Edge resolution (unconditional, conditional, parallel)
- Async node execution
- Parallel group execution via `asyncio.gather()`
- Iteration counting and safety valve
- CLI subprocess helpers (`claude_node`, `codex_node`, `gemini_node`)

### What stays in dev-loop.py

- Prompt generator functions (`_implementation_prompt()`, etc.) — now return template strings with `{key}` placeholders
- GitHub helpers (`gh_comment`, `wait_for_ci`, `extract_pr_url`, etc.)
- `RunContext` for logging, status files, notifications (wired via event callbacks)
- CLI argument parsing
- Worktree setup logic
- PR comment side-effects (in `on_node_start`/`on_node_end` callbacks or as explicit `python_node` steps)

### Graph definition (dev-loop.py)

```python
graph = StateGraph(max_iterations=5, cwd=worktree_path)

# Phase 1: Implementation
graph.add_node("worktree_setup", python_node(setup_worktree))
graph.add_node("implement", claude_node(
    _implementation_prompt(),
    output_key="implementation_output",
))
graph.add_node("smoke_test", claude_node(
    _smoke_test_prompt(),
    output_key="smoke_test_output",
))
graph.add_node("smoke_test_fix", claude_node(
    _smoke_test_fix_prompt(),
    output_key="smoke_test_fix_output",
))
graph.add_node("smoke_test_retry", claude_node(
    _smoke_test_prompt(),
    output_key="smoke_test_retry_output",
))
graph.add_node("create_pr", python_node(create_and_push_pr))

graph.add_edge("start", "worktree_setup")
graph.add_edge("worktree_setup", "implement")
graph.add_edge("implement", "smoke_test")
graph.add_conditional_edges("smoke_test", smoke_test_result, {
    "pass": "create_pr",
    "fail": "smoke_test_fix",
})
graph.add_edge("smoke_test_fix", "smoke_test_retry")
graph.add_conditional_edges("smoke_test_retry", smoke_test_result, {
    "pass": "create_pr",
    "fail": "create_pr",  # give up after one retry
})
graph.add_edge("create_pr", "simplify")

# Phase 2: Review loop
graph.add_node("simplify", claude_node(
    _simplify_prompt(),
    output_key="simplify_output",
))
graph.add_node("simplify_commit", python_node(commit_and_push))
graph.add_node("code_review", claude_node(
    _code_review_prompt(),
    output_key="code_review_output",
))
graph.add_node("security_review", claude_node(
    _security_review_prompt(),
    output_key="security_review_output",
))
graph.add_node("wait_for_ci", python_node(wait_for_ci_checks))
graph.add_node("decision", claude_node(
    _decision_prompt(),
    output_key="decision_output",
))
graph.add_node("fix", claude_node(
    _fix_prompt(),
    output_key="fix_output",
))

graph.add_edge("simplify", "simplify_commit")
graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
graph.add_edge("code_review", "wait_for_ci")
graph.add_edge("security_review", "wait_for_ci")
graph.add_edge("wait_for_ci", "decision")
graph.add_conditional_edges("decision", needs_fix, {"fix": "fix", "done": END})
graph.add_edge("fix", "simplify")  # loop back

# Wire up observability
@graph.on_node_start
async def on_start(node_name, state):
    run_ctx.update_status(f"{node_name} | Running")

@graph.on_node_end
async def on_end(node_name, state):
    run_ctx.log(f"Finished {node_name}")

# Run based on mode
if args.review_only:
    await graph.run(initial_state=state, start_node="simplify")
elif args.continue_pr:
    await graph.run(initial_state=state, start_node="implement")
else:
    await graph.run(initial_state=state)
```

## Implementation notes

- Engine uses only stdlib (`asyncio`, `dataclasses`) + optionally `pydantic` for future typed state
- CLI nodes use `asyncio.create_subprocess_exec` for non-blocking execution
- `claude_node()` strips `CLAUDECODE` and `ANTHROPIC_API_KEY` from the subprocess environment (prevents nested session detection, forces Max subscription)
- The existing `run_claude()` / `run_claude_bg()` helpers get refactored into `claude_node()` internals
- Unit tests for the engine use mock async functions, no real subprocess calls
- Integration tested by running the full dev-loop workflow
- `codex_node()` and `gemini_node()` are included in the engine API but may be stubbed in v1 (only `claude_node()` is exercised by dev-loop)
