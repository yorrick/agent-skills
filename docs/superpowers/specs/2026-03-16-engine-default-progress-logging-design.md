# Design: Default Progress Logging in StateGraph Engine

## Problem

When `/workflow` scripts run, there is zero feedback during execution. A `claude_node` can take 10+ minutes and the user sees nothing — just a blank terminal. The main `dev-loop.py` has full observability via `RunContext` + event callbacks, but ad-hoc `/workflow` scripts don't wire up any callbacks, so the engine runs silently.

## Solution

Add built-in default progress logging to `StateGraph.run()` that activates automatically when no external callbacks are registered. This means every workflow script gets progress feedback without any code changes.

## Architecture

### Detection logic

In `StateGraph.run()`, before the main loop, check whether `_on_node_start` and `_on_node_end` callback lists are empty. If empty, use built-in default logging to stderr. If callbacks are registered (as `dev-loop.py` does), skip default logging entirely — the caller owns observability.

### Default logging format

Output goes to **stderr** (not stdout) so it doesn't pollute the script's result output.

```
[workflow] Starting: implement
[workflow] Finished: implement (2m15s)
[workflow] Starting: test
[workflow] Finished: test (0m03s)
```

### Elapsed time tracking

Track `time.monotonic()` at the start of each node. Compute elapsed on node end. Format as `Xm YYs` (e.g., `2m15s`, `0m03s`).

### Error logging

On error, always print to stderr regardless of whether callbacks exist:
```
[workflow] ERROR in node_name: error message
```

### What doesn't change

- `dev-loop.py` already registers `on_node_start`/`on_node_end`/`on_error` callbacks via `RunContext`. Those still work exactly as before — no default logging fires.
- The `/workflow` template in `workflow.md` stays the same. The progress output now comes for free.
- `StateGraph` API is unchanged — no new constructor parameters needed.

## Affected files

1. `dev-loop/scripts/engine.py` — Add default stderr logging in `run()` and `_run_node()` (for parallel nodes)
2. `dev-loop/tests/test_engine.py` — Add tests for default logging behavior

## Validation

1. Run existing tests: `uv run dev-loop/tests/test_engine.py` — all must pass
2. Run integration test: `uv run dev-loop/tests/test_integration.py` — must pass
3. Create a simple test workflow script that runs a `shell_node("echo hello")` and verify stderr shows progress lines
4. Verify that when callbacks ARE registered, no default logging appears
