# Stream Workflow Engine Logs in Real Time

**Issue:** [#15](https://github.com/yorrickjansen/claude-code-plugins/issues/15)
**Date:** 2026-03-16
**Status:** Implemented

## Problem

The integration test (`test_workflow_integration.py`) captures workflow engine output into a buffer and only displays it after the workflow finishes. During execution (10-15 minutes), you only see generic `[..] Running... (elapsed: Xm)` lines with no visibility into what's happening. The engine already emits `[workflow]` log lines to stderr, but the test's `select` loop reads them into `output_lines` without printing them.

Additionally, parallel nodes (e.g., `code_review` + `security_review`) emit logs with the same `[workflow]` prefix, making it impossible to distinguish which node produced which output when logs interleave.

## Design

### Engine log format change

In `engine.py`, change all default log lines to include the node name in the prefix. The node name is already available at every call site.

**Before:**
```
[workflow] Starting: implement
[workflow] Finished: implement (0m45s)
[workflow] ERROR in implement: something broke
```

**After:**
```
[workflow:implement] Starting
[workflow:implement] Finished (0m45s)
[workflow:implement] ERROR: something broke
```

This applies consistently to all nodes (sequential and parallel), not just parallel ones.

**Files:** `dev-loop/scripts/engine.py` — six `print` statements: three in `run()` and three in `_run_node()`.

### Test harness streaming

In `test_workflow_integration.py`'s `main()` function, add `print(line, end="", flush=True)` at both output capture sites:
1. Inside the `select` loop (where lines arrive during execution)
2. In the post-loop drain (where late-arriving lines are read after the process exits)

This echoes every captured line to the terminal in real time while still collecting output for assertions.

The existing `[..] Running... (elapsed: Xm)` heartbeat ticker remains unchanged.

**Files:** `dev-loop/tests/test_workflow_integration.py` — the `main()` function's process output loop.

### Assertion updates

Update test assertions in **both** test files to match the new log format:

**`test_workflow_integration.py`:**

| Before | After |
|--------|-------|
| `"[workflow] Starting:" in stdout` | `"[workflow:" in stdout and "] Starting" in stdout` |
| `"[workflow] Finished:" in stdout` | `"[workflow:" in stdout and "] Finished" in stdout` |
| `f"[workflow] Starting: {name}" in stdout` | `f"[workflow:{name}] Starting" in stdout` |

**`test_engine.py`:**

| Before | After |
|--------|-------|
| `"[workflow] Starting: a" in captured.err` | `"[workflow:a] Starting" in captured.err` |
| `"[workflow] Finished: a" in captured.err` | `"[workflow:a] Finished" in captured.err` |
| `"[workflow] ERROR in boom:" in captured.err` | `"[workflow:boom] ERROR:" in captured.err` |

The `test_default_logging_suppressed_with_callbacks` test asserts `"[workflow]" not in captured.err` — this still passes because `[workflow:a]` does not contain the exact substring `[workflow]` (with closing bracket). No change needed.

No other consumers of this log format exist — only the two test files parse these patterns.

## Scope

- Change log format in engine default logging (6 print statements)
- Echo lines in test harness (2 sites)
- Update test assertions in both test files to match new format
- No new abstractions, callbacks, or dependencies
