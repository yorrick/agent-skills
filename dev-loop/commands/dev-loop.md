---
description: "Full development loop: brainstorm, plan, implement, PR, then iterative review (simplify + code review + security review) until clean"
argument-hint: "[--max-iterations N] [--skip-permissions]"
allowed-tools: ["Bash(uv run ${CLAUDE_PLUGIN_ROOT}/scripts/*)", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Skill"]
---

# Development Loop

You are orchestrating a full feature development cycle. Follow these phases exactly.

## Phase 0: Check dependencies

First, verify all required plugins are installed. Run this using the Bash tool:

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" --help

If uv or the script fails, tell the user they need uv installed (https://docs.astral.sh/uv/).

## Phase 1: Brainstorm (interactive)

Invoke the superpowers:brainstorming skill and follow it exactly. This is interactive — ask the user questions, explore approaches, and get design approval.

The brainstorming skill will transition to the writing-plans skill automatically. Follow that too — produce a complete implementation plan saved to `docs/plans/`.

Note the plan file path when done.

## Phase 2: Hand off to automated loop

Once the plan is written and the user approves it, run the dev-loop orchestrator script.

Before running the script, confirm with the user:
- Show them the plan file path
- Show the command that will be run
- Ask if they want to adjust --max-iterations (default 3) or use --skip-permissions

Then execute the script using the Bash tool:

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" PLAN_FILE_PATH $ARGUMENTS

Replace PLAN_FILE_PATH with the actual path to the plan file created in Phase 1.

The script will:
1. Implement the plan (using executing-plans skill) including running lint, typecheck, format, and tests
2. Create a PR
3. Run a review loop:
   - /simplify — clean up the code
   - /code-review:code-review + /security-review — in parallel
   - If Critical/Important issues found: fix and loop
   - If clean: done
