#!/usr/bin/env bash
# dev-loop.sh — Automated development loop: implement → simplify → review → fix → repeat
#
# Usage: dev-loop.sh <plan-file> [--max-iterations N] [--pr-url URL] [--skip-permissions]
#
# If --pr-url is provided, skips implementation and goes straight to the review loop.
# Otherwise, implements the plan, creates a PR, then enters the review loop.

set -euo pipefail

# --- Parse arguments ---
PLAN_FILE=""
MAX_ITERATIONS=3
PR_URL=""
PERMISSION_MODE="default"

while [[ $# -gt 0 ]]; do
  case $1 in
    --max-iterations)
      MAX_ITERATIONS="$2"
      shift 2
      ;;
    --pr-url)
      PR_URL="$2"
      shift 2
      ;;
    --skip-permissions)
      PERMISSION_MODE="bypassPermissions"
      shift
      ;;
    -h|--help)
      cat <<'HELP'
dev-loop.sh — Automated development loop

USAGE:
  dev-loop.sh <plan-file> [OPTIONS]

ARGUMENTS:
  plan-file               Path to the implementation plan (from /write-plan)

OPTIONS:
  --max-iterations N      Max review iterations (default: 3)
  --pr-url URL            Skip implementation, review existing PR
  --skip-permissions      Run with bypassPermissions mode
  -h, --help              Show this help

EXAMPLES:
  dev-loop.sh docs/plans/2026-03-06-auth.md
  dev-loop.sh docs/plans/2026-03-06-auth.md --max-iterations 5
  dev-loop.sh docs/plans/2026-03-06-auth.md --pr-url https://github.com/org/repo/pull/42
HELP
      exit 0
      ;;
    *)
      if [[ -z "$PLAN_FILE" ]]; then
        PLAN_FILE="$1"
      else
        echo "Error: unexpected argument '$1'" >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ -z "$PLAN_FILE" ]]; then
  echo "Error: plan file required. Usage: dev-loop.sh <plan-file> [OPTIONS]" >&2
  exit 1
fi

if [[ ! -f "$PLAN_FILE" ]]; then
  echo "Error: plan file not found: $PLAN_FILE" >&2
  exit 1
fi

CLAUDE_ARGS=()
if [[ "$PERMISSION_MODE" != "default" ]]; then
  CLAUDE_ARGS+=(--permission-mode "$PERMISSION_MODE")
fi

log() {
  echo ""
  echo "================================================================"
  echo "  $1"
  echo "================================================================"
  echo ""
}

run_claude() {
  local description="$1"
  local prompt="$2"
  local output_file="$3"

  log "$description"
  (unset CLAUDECODE; claude -p "$prompt" "${CLAUDE_ARGS[@]}" --output-format json > "$output_file" 2>&1) || true
  echo "  Output saved to: $output_file"
}

extract_result() {
  local json_file="$1"
  python3 -c "
import sys, json
try:
    data = json.load(open('$json_file'))
    print(data.get('result', data.get('message', json.dumps(data))))
except:
    print(open('$json_file').read())
" 2>/dev/null || cat "$json_file"
}

WORK_DIR=$(mktemp -d)
echo "Work directory: $WORK_DIR"

# --- Phase 1: Implementation (skip if --pr-url provided) ---
if [[ -z "$PR_URL" ]]; then
  run_claude \
    "Phase 1: Implementing plan" \
    "Read the plan at $PLAN_FILE. Use the superpowers:executing-plans skill to implement it task by task.

After completing all tasks, discover and run the project's quality gates:
- Check package.json scripts, Makefile, pyproject.toml, tox.ini, Cargo.toml, or equivalent
- Run linting (eslint, ruff, pylint, clippy, etc.)
- Run type checking (tsc, mypy, pyright, etc.)
- Run formatting check (prettier, black, rustfmt, etc.)
- Run the test suite

Fix any failures before proceeding. Once everything passes, commit all work." \
    "$WORK_DIR/implementation.json"

  run_claude \
    "Phase 1b: Creating PR" \
    "Push the current branch and create a pull request using gh pr create. Use a descriptive title and body summarizing what was implemented based on the plan at $PLAN_FILE. Return the PR URL." \
    "$WORK_DIR/pr-creation.json"

  # Extract PR URL from output
  PR_URL=$(grep -oE 'https://github\.com/[^"[:space:]]+/pull/[0-9]+' "$WORK_DIR/pr-creation.json" | head -1 || true)

  if [[ -z "$PR_URL" ]]; then
    echo "Error: could not extract PR URL from output. Check $WORK_DIR/pr-creation.json" >&2
    exit 1
  fi

  echo "PR created: $PR_URL"
fi

# --- Phase 2: Review loop ---
ITERATION=0
while [[ $ITERATION -lt $MAX_ITERATIONS ]]; do
  ITERATION=$((ITERATION + 1))
  log "Review iteration $ITERATION of $MAX_ITERATIONS"

  # Step 1: Simplify
  run_claude \
    "Step 1/$ITERATION: Simplify" \
    "/simplify" \
    "$WORK_DIR/simplify-$ITERATION.json"

  # Commit any simplify fixes
  run_claude \
    "Step 1b/$ITERATION: Commit simplify fixes" \
    "If there are any uncommitted changes from the simplify pass, commit them with a descriptive message and push to the current branch." \
    "$WORK_DIR/simplify-commit-$ITERATION.json"

  # Step 2: Code review + Security review in parallel
  log "Step 2/$ITERATION: Code review + Security review (parallel)"

  (unset CLAUDECODE; claude -p "/code-review:code-review $PR_URL" "${CLAUDE_ARGS[@]}" --output-format json \
    > "$WORK_DIR/code-review-$ITERATION.json" 2>&1) &
  CODE_REVIEW_PID=$!

  (unset CLAUDECODE; claude -p "/security-review" "${CLAUDE_ARGS[@]}" --output-format json \
    > "$WORK_DIR/security-review-$ITERATION.json" 2>&1) &
  SECURITY_REVIEW_PID=$!

  wait $CODE_REVIEW_PID || true
  wait $SECURITY_REVIEW_PID || true

  echo "  Code review: $WORK_DIR/code-review-$ITERATION.json"
  echo "  Security review: $WORK_DIR/security-review-$ITERATION.json"

  # Step 3: Decision gate
  CODE_REVIEW_TEXT=$(extract_result "$WORK_DIR/code-review-$ITERATION.json")
  SECURITY_REVIEW_TEXT=$(extract_result "$WORK_DIR/security-review-$ITERATION.json")

  run_claude \
    "Step 3/$ITERATION: Decision gate" \
    "Based on these review findings, are there Critical or Important issues that MUST be fixed before merging?

Code Review findings:
$CODE_REVIEW_TEXT

Security Review findings:
$SECURITY_REVIEW_TEXT

Answer with EXACTLY one word: YES or NO. Only answer YES if there are genuinely Critical or Important issues. Minor suggestions and nitpicks do not count." \
    "$WORK_DIR/decision-$ITERATION.json"

  DECISION=$(extract_result "$WORK_DIR/decision-$ITERATION.json")

  if echo "$DECISION" | grep -qi "NO"; then
    log "No critical issues found. PR is ready!"
    echo "PR: $PR_URL"
    echo "Review artifacts: $WORK_DIR"
    exit 0
  fi

  log "Step 4/$ITERATION: Fixing issues"

  run_claude \
    "Step 4/$ITERATION: Fix issues" \
    "The following issues were found during review of PR $PR_URL. Fix all Critical and Important issues. After fixing, run the project's quality gates (lint, typecheck, format, tests) and make sure everything passes. Commit and push the fixes.

Code Review findings:
$CODE_REVIEW_TEXT

Security Review findings:
$SECURITY_REVIEW_TEXT" \
    "$WORK_DIR/fix-$ITERATION.json"

done

log "Max iterations ($MAX_ITERATIONS) reached. Review PR manually."
echo "PR: $PR_URL"
echo "Review artifacts: $WORK_DIR"
exit 1
