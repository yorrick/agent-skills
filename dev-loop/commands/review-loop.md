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
3. Wait for CI/CD checks to complete (if any exist)
4. If Critical/Important issues or CI failures found: fix and loop
5. If clean and CI passing: done

Monitor progress from another terminal:
- Status: `watch -n1 cat .dev-loop/latest/status.txt`
- Full log: `tail -f .dev-loop/latest/dev-loop.log`
