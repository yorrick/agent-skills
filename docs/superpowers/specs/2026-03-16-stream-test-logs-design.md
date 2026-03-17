# Stream Workflow Engine Logs in Real Time

**Issue:** [#15](https://github.com/yorrickjansen/claude-code-plugins/issues/15)
**Date:** 2026-03-16
**Status:** Approved

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

**Files:** `dev-loop/scripts/engine.py` — `_run_node()` method and `_execute()` default logging.

### Test harness streaming

In `test_workflow_integration.py`, add `print(line, end="", flush=True)` alongside `output_lines.append(line)` in the `select` loop. This echoes every captured line to the terminal in real time while still collecting output for assertions.

The existing `[..] Running... (elapsed: Xm)` heartbeat ticker remains unchanged.

**Files:** `dev-loop/tests/test_workflow_integration.py` — the `select` loop in the test function.

### Assertion updates

Update test assertions to match the new log format: look for `[workflow:node_name]` instead of `[workflow] Starting: node_name`.

## Scope

- Change log format in engine default logging (~5 lines)
- Echo lines in test harness (~1 line)
- Update test assertions to match new format
- No new abstractions, callbacks, or dependencies
