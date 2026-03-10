---
description: "Full development loop: brainstorm, plan, implement, PR, then iterative review (simplify + code review + security review) until clean"
argument-hint: "<feature description>"
allowed-tools: ["Bash(uv run ${CLAUDE_PLUGIN_ROOT}/scripts/*)", "Bash(gh *)", "Read", "Write", "Edit", "Glob", "Grep", "Agent", "Skill"]
---

# Development Loop

You are orchestrating a full feature development cycle.

The user's feature request is: $ARGUMENTS

Follow these phases exactly.

## Phase 0: Check dependencies

Verify the script exists by running this using the Bash tool:

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" --help

If uv or the script fails, tell the user they need uv installed (https://docs.astral.sh/uv/).

## Phase 1: Brainstorm (interactive)

Invoke the superpowers:brainstorming skill and follow it exactly. Use the feature request above as the starting point. This is interactive — ask the user questions, explore approaches, and get design approval.

The brainstorming skill will transition to the writing-plans skill automatically. Follow that too — produce a complete implementation plan saved to `docs/plans/`.

Note the plan file path when done.

## Phase 1b: Create GitHub issue with the plan

After the plan is written and approved:

1. Read the plan file content
2. Create a GitHub issue using the gh CLI with the plan as the body:

gh issue create --title "<descriptive title from the plan>" --body "$(cat <plan-file-path>)"

3. Note the issue URL returned by gh. This will be passed to the script so all implementation steps reference the GitHub issue as the source of truth.

## Phase 2: Hand off to automated loop

Once the issue is created, run the dev-loop orchestrator script.

Before running the script:
1. Check if the current branch already has an open PR: `gh pr view --json url --jq .url`
2. If a PR exists, tell the user and recommend using `--continue-pr` to implement on the current branch and review against the existing PR
3. Show them the GitHub issue URL
4. Show the exact command that will be run
5. Ask if they want to adjust --max-iterations (default 3), use --skip-permissions, or set --reviewers

Then execute the script using the Bash tool. IMPORTANT: pass the issue URL (not the plan file path) and options to the script. Do NOT pass the feature description — that was only for Phase 1.

Default mode (new branch + new PR):

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" <issue-url> [--max-iterations N] [--skip-permissions] [--reviewers user1,user2,org/team]

Continue mode (existing branch + existing PR):

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" <issue-url> --continue-pr [--max-iterations N] [--skip-permissions] [--reviewers user1,user2,org/team]

Review-only mode (skip implementation, just review):

uv run "${CLAUDE_PLUGIN_ROOT}/scripts/dev-loop.py" <issue-url> --review-only <pr-url> [--max-iterations N] [--skip-permissions] [--reviewers user1,user2,org/team]

The script will:
1. Create a feature branch and worktree (default mode) or use the current branch (--continue-pr)
2. Fetch the plan from the GitHub issue
3. Implement the plan (using executing-plans skill) including running lint, typecheck, format, and tests
4. Create a PR linked to the issue (default mode) or push to the existing PR (--continue-pr)
5. Run a review loop:
   - /simplify — clean up the code
   - /code-review:code-review + /security-review — in parallel
   - Wait for CI/CD checks to complete (if any exist)
   - If Critical/Important issues or CI failures found: fix and loop
   - If clean and CI passing: done

Monitor progress from another terminal:
- Status: `watch -n1 cat .dev-loop/latest/status.txt`
- Full log: `tail -f .dev-loop/latest/dev-loop.log`
