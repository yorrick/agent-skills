# Live Feedback & Observability for dev-loop

## Problem

When dev-loop runs, you can't monitor progress from outside the Claude session, and the in-session output is a mix of JSON and unstructured log lines. Log files go to an unpredictable `/tmp` path. There's no way to check in from a separate terminal or get notified when milestones happen.

## Design

Five components, all additive changes to `dev-loop.py`:

### 1. Project-local log directory

All output goes to `.dev-loop/runs/<YYYY-MM-DD-HHMMSS>/` in the project root (via `git rev-parse --show-toplevel`). Inside:

```
.dev-loop/
  latest -> runs/2026-03-09-143022/   # symlink to most recent run
  runs/
    2026-03-09-143022/
      status.txt
      dev-loop.log
      worktree-setup.json
      implementation.json
      pr-creation.json
      simplify-1.json
      code-review-1.json
      security-review-1.json
      decision-1.json
      fix-1.json
```

Auto-add `.dev-loop/` to `.gitignore` if not already present.

### 2. Status file

`status.txt` — single line, overwritten at each phase transition.

Format: `<phase> | <detail> | <elapsed>`

Examples:
```
Phase 0 | Setting up worktree | 0:00:12
Phase 1 | Implementing plan | 0:03:45
Review 2/3 | Code review + Security review (parallel) | 0:14:01
Done | No critical issues after 2 iterations | 0:22:10
Error | Implementation failed | 0:05:12
```

Watchable via `watch -n1 cat .dev-loop/latest/status.txt`.

### 3. Human-readable log

`dev-loop.log` — append-only, timestamped key events.

```
[2026-03-09 14:30:22] START dev-loop for https://github.com/user/repo/issues/42
[2026-03-09 14:30:22] Run directory: .dev-loop/runs/2026-03-09-143022
[2026-03-09 14:30:22] Options: max_iterations=3, skip_permissions=true, reviewers=alice
[2026-03-09 14:30:22] PHASE 0: Setting up worktree
[2026-03-09 14:30:35] Worktree created at /Users/.../worktrees/dev-loop-issue-42
[2026-03-09 14:30:35] PHASE 1: Implementing plan
[2026-03-09 14:38:57] Implementation complete
[2026-03-09 14:39:41] REVIEW 1/3: Starting
[2026-03-09 14:44:30] REVIEW 1/3: Decision — YES (issues found)
[2026-03-09 14:51:10] DONE: PR ready after 2 iterations
```

Key events only — no full review text or JSON dumps.

### 4. macOS notifications

Fire-and-forget `osascript -e 'display notification "..." with title "dev-loop"'` at high-signal moments only:

| Event | Notification |
|---|---|
| PR created | `PR #87 created — starting review loop` |
| Issues found | `Review 1/3: Critical issues found, fixing...` |
| CI failed | `Review 2/3: CI failed, fixing...` |
| Success | `PR #87 ready for review after 2 iterations` |
| Max iterations | `PR #87 needs manual review (3 iterations exhausted)` |
| Error/abort | `dev-loop aborted: <reason>` |

Silent failure if `osascript` is unavailable. No platform detection — just try/except.

### 5. External dependency awareness in planning

Not a code change to the plugin. Instead, during brainstorming/planning phases, the workflow should explicitly:

1. Identify every external system the feature touches
2. Specify failure modes per dependency
3. Define handling strategy per failure mode (retry, circuit breaker, fallback, fail fast)
4. Define observability requirements (logging, metrics, alerts)

Plan documents should include a section:

```markdown
## External Dependencies & Failure Handling

| Dependency | Failure modes | Strategy |
|---|---|---|
| Stripe API | timeout, 429, 500 | Retry 3x with exponential backoff, then fail with user-facing error |
| Redis cache | connection refused, timeout | Degrade to DB lookup, log warning |
```

This is enforced by updating the brainstorming/planning prompts to include external dependency analysis as a required concern.
