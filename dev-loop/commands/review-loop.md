---
description: "Run the review loop on an existing PR: simplify, code review + security review in parallel, fix issues, repeat"
argument-hint: "<issue-url> --pr-url <PR-URL> [--max-iterations N] [--skip-permissions]"
allowed-tools: ["Bash(uv run ${CLAUDE_PLUGIN_ROOT}/scripts/*)"]
---

# Review Loop

Run the review loop on an existing PR. This skips brainstorming and implementation — it goes straight to the iterative review cycle.

Execute the dev-loop script with the --pr-url flag using the Bash tool:

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" $ARGUMENTS

The script will:
1. Run /simplify and commit fixes
2. Run /code-review:code-review + /security-review in parallel
3. If Critical/Important issues found: fix and loop
4. If clean: done
