# Implementation Plan: Default Progress Logging in StateGraph Engine

## Overview

Add built-in default progress logging to `StateGraph.run()` that prints node start/finish to stderr when no external callbacks are registered. This gives all `/workflow` scripts automatic progress feedback.

## Step 1: Add default logging to `engine.py`

**File:** `dev-loop/scripts/engine.py`

### 1a. Add `import time` and `import sys` at the top

Both are stdlib — no dependency changes needed. `sys` is already used in some scripts but not in engine.py itself.

### 1b. Add a `_format_elapsed` helper function

```python
def _format_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"
```

Place after the `_SafeFormatMap` class, before `_EndSentinel`.

### 1c. Add default logging in `run()` method

At the start of `run()`, determine whether to use default logging:

```python
use_default_log = not self._on_node_start and not self._on_node_end
```

In the main loop, wrap node execution with stderr progress lines when `use_default_log` is True:

- Before `await self._emit_node_start(...)`: if `use_default_log`, print `[workflow] Starting: {current}` to stderr and record `time.monotonic()`
- After `await self._emit_node_end(...)`: if `use_default_log`, print `[workflow] Finished: {current} ({elapsed})` to stderr
- In the `except` block: if `use_default_log`, print `[workflow] ERROR in {current}: {error}` to stderr

### 1d. Add default logging in `_run_node()` for parallel nodes

Same logic — `_run_node()` is used for parallel execution. Apply the same `use_default_log` check (pass it as a parameter or check the callback lists directly).

Since `_run_node` is an instance method it can access `self._on_node_start` and `self._on_node_end` directly.

## Step 2: Add tests for default logging

**File:** `dev-loop/tests/test_engine.py`

### 2a. Test: default logging appears when no callbacks registered

- Build a simple graph: `start -> a -> END`
- Run it with `capsys` or by capturing stderr
- Assert stderr contains `[workflow] Starting: a` and `[workflow] Finished: a`

### 2b. Test: default logging suppressed when callbacks registered

- Build the same graph
- Register a dummy `on_node_start` callback
- Run and assert stderr does NOT contain `[workflow]` lines

### 2c. Test: error logging appears on node failure

- Build a graph with a node that raises an exception
- Run and catch the exception
- Assert stderr contains `[workflow] ERROR in`

## Step 3: Run validation

1. `uv run dev-loop/tests/test_engine.py` — all tests pass
2. `uv run dev-loop/tests/test_integration.py` — integration test passes
3. `uv run ruff check dev-loop/` — no lint issues
4. `uv run pyright` — no type errors

## Validation

To verify the feature works end-to-end, create a minimal workflow script:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mermaid-ascii"]
# ///
import sys
sys.path.insert(0, "<plugin-root>/scripts")
import asyncio
from engine import StateGraph, shell_node, END

async def main():
    graph = StateGraph()
    graph.add_node("hello", shell_node("echo hello world", output_key="out"))
    graph.add_edge("start", "hello")
    graph.add_edge("hello", END)
    result = await graph.run()
    print(result.get("out", ""))

asyncio.run(main())
```

Expected stderr output:
```
[workflow] Starting: hello
[workflow] Finished: hello (0m00s)
```

Expected stdout: `hello world`
