#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""The fork check's PreToolUse hook: stops a warm-up before its first tool runs.

`replay.run_claude` passes the same `--settings` (naming this script) to the
warm-up and to the job, because a prompt-cache entry ends with the exact request
that wrote it: the job's first request reads the warm-up's entry only if both
send the very same request, flags included. Only the environment tells them
apart. In the warm-up (JEV_FORK_CHECK_WARMUP=1) this prints a decision that
denies the tool and stops Claude Code, so the warm-up writes the shared
conversation to the cache and changes nothing. In the job it prints nothing, so
the job runs exactly as it would without the hook.

It reads nothing and needs nothing beyond the standard library, so the runner
can start it with its own interpreter, never through the user's shell or PATH.
"""

import json
import os
import sys

STOP = {
    "continue": False,
    "stopReason": "fork-check warm-up",
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "fork-check warm-up: no tools",
    },
}


def main() -> int:
    if os.environ.get("JEV_FORK_CHECK_WARMUP") == "1":
        sys.stdout.write(json.dumps(STOP))
    return 0


if __name__ == "__main__":
    sys.exit(main())
