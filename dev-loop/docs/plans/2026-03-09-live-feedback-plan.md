# Live Feedback & Observability Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add project-local logging, a watchable status file, and macOS notifications to dev-loop so users can monitor progress both in-session and from external terminals.

**Architecture:** Replace the temp-dir-based output with a project-local `.dev-loop/runs/<timestamp>/` directory. Introduce a `RunContext` class that manages the run directory, status file, log file, and notifications. All existing `log()` and `print()` calls route through `RunContext` methods instead.

**Tech Stack:** Python 3.10+, no new dependencies (uses stdlib `subprocess` for `osascript`, `datetime` for timestamps, `pathlib` for file ops)

---

### Task 1: Add RunContext class with run directory setup

**Files:**
- Modify: `scripts/dev-loop.py:11-22` (imports)
- Modify: `scripts/dev-loop.py:25-26` (replace `log` function)
- Modify: `scripts/dev-loop.py:424-452` (main function setup)

**Step 1: Write the RunContext class**

Add after imports (line 22), replacing the existing `log()` function. The class handles:
- Creating `.dev-loop/runs/<YYYY-MM-DD-HHMMSS>/` in the git repo root
- Creating a `.dev-loop/latest` symlink pointing to the current run
- Adding `.dev-loop/` to `.gitignore` if absent
- Providing the run directory as `Path` for output files

```python
from datetime import datetime, timezone


class RunContext:
    """Manages run directory, status file, log file, and notifications."""

    def __init__(self) -> None:
        repo_root = self._git_root()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        self._run_dir = repo_root / ".dev-loop" / "runs" / timestamp
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._run_dir / "dev-loop.log"
        self._status_file = self._run_dir / "status.txt"
        self._start_time = time.monotonic()

        # Create/update latest symlink
        latest = repo_root / ".dev-loop" / "latest"
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(self._run_dir)

        # Ensure .dev-loop/ is in .gitignore
        gitignore = repo_root / ".gitignore"
        if gitignore.exists():
            content = gitignore.read_text()
            if ".dev-loop/" not in content:
                with open(gitignore, "a") as f:
                    f.write("\n.dev-loop/\n")
        else:
            gitignore.write_text(".dev-loop/\n")

    @staticmethod
    def _git_root() -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
        return Path.cwd()

    @property
    def dir(self) -> Path:
        return self._run_dir

    def _elapsed(self) -> str:
        seconds = int(time.monotonic() - self._start_time)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
```

**Step 2: Update main() to use RunContext instead of tempfile**

Replace lines 451-452:
```python
# OLD:
work_dir = Path(tempfile.mkdtemp(prefix="dev-loop-"))
print(f"Work directory: {work_dir}")

# NEW:
ctx = RunContext()
work_dir = ctx.dir
print(f"Run directory: {work_dir}")
```

**Step 3: Remove the `tempfile` import**

It's no longer needed — remove `import tempfile` from line 19.

**Step 4: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS (or only pre-existing issues)

**Step 5: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: add RunContext with project-local run directory and latest symlink"
```

---

### Task 2: Add status file and log file methods to RunContext

**Files:**
- Modify: `scripts/dev-loop.py` (RunContext class)

**Step 1: Add status(), log(), and the phase-logging method**

Add these methods to `RunContext`:

```python
    def status(self, phase: str, detail: str) -> None:
        """Overwrite status.txt with current phase info."""
        line = f"{phase} | {detail} | {self._elapsed()}"
        self._status_file.write_text(line + "\n")
        # Also print to stdout for in-session visibility
        print(f"\n{'=' * 64}\n  {phase}: {detail}\n{'=' * 64}\n", flush=True)

    def log(self, message: str) -> None:
        """Append a timestamped line to dev-loop.log."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        with open(self._log_file, "a") as f:
            f.write(line + "\n")
        print(f"  {message}", flush=True)
```

**Step 2: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS

**Step 3: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: add status file and log file methods to RunContext"
```

---

### Task 3: Add notification method to RunContext

**Files:**
- Modify: `scripts/dev-loop.py` (RunContext class)

**Step 1: Add notify() method**

```python
    def notify(self, message: str) -> None:
        """Send a macOS notification. Silently fails on non-macOS or if osascript unavailable."""
        try:
            subprocess.run(
                [
                    "osascript", "-e",
                    f'display notification "{message}" with title "dev-loop"',
                ],
                capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
```

**Step 2: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS

**Step 3: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: add macOS notification method to RunContext"
```

---

### Task 4: Wire up RunContext throughout main()

**Files:**
- Modify: `scripts/dev-loop.py:424-628` (main function)

This is the largest task. Replace all `log()` calls and relevant `print()` calls with `ctx.status()`, `ctx.log()`, and `ctx.notify()`. Also remove the old top-level `log()` function.

**Step 1: Delete the old `log()` function**

Remove lines 25-26 (the standalone `def log(msg)` function).

**Step 2: Add startup logging in main()**

After `ctx = RunContext()`, add:
```python
ctx.log(f"START dev-loop for {issue_url}")
ctx.log(f"Run directory: {work_dir}")
ctx.log(f"Options: max_iterations={args.max_iterations}, skip_permissions={args.skip_permissions}, reviewers={args.reviewers}")
```

**Step 3: Replace all phase transitions with ctx.status() + ctx.log()**

Apply these replacements throughout `main()`:

| Old call | New calls |
|---|---|
| `log("Phase 0: Setting up branch and worktree")` | `ctx.status("Phase 0", "Setting up worktree")` and `ctx.log("PHASE 0: Setting up worktree")` |
| `log("Phase 1: Implementing plan")` | `ctx.status("Phase 1", "Implementing plan")` and `ctx.log("PHASE 1: Implementing plan")` |
| `log("Phase 1b: Creating PR")` | `ctx.status("Phase 1b", "Creating PR")` and `ctx.log("PHASE 1b: Creating PR")` |
| `print(f"PR created: {pr_url}")` | `ctx.log(f"PR created: {pr_url}")` |
| `log(f"Review iteration {iteration} of {args.max_iterations}")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Starting")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Starting")` |
| `log(f"Step 1/{iteration}: Simplify")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Simplify")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Simplify")` |
| `log(f"Step 2/{iteration}: Code review + Security review (parallel)")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Code review + Security review (parallel)")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Code review + Security review (parallel)")` |
| `log(f"Step 2b/{iteration}: Checking CI/CD status")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Checking CI/CD")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Checking CI/CD")` |
| `log(f"Step 3/{iteration}: Decision gate")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Decision gate")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Decision gate")` |
| `log(f"Step 4/{iteration}: Fixing issues")` | `ctx.status(f"Review {iteration}/{args.max_iterations}", "Fixing issues")` and `ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Fixing issues")` |

**Step 4: Add notifications at key milestones**

| Location | Notification |
|---|---|
| After PR created (line ~494) | `ctx.notify(f"PR #{pr_number} created — starting review loop")` |
| Decision = YES (issues found) | `ctx.notify(f"Review {iteration}/{max}: Critical issues found, fixing...")` |
| CI failed (line ~581) | `ctx.notify(f"Review {iteration}/{max}: CI failed, fixing...")` |
| Decision = NO (success, line ~592) | `ctx.notify(f"PR #{pr_number} ready for review after {iteration} iterations")` and `ctx.status("Done", f"No critical issues after {iteration} iterations")` and `ctx.log(f"DONE: PR ready after {iteration} iterations")` |
| Max iterations reached (line ~614) | `ctx.notify(f"PR #{pr_number} needs manual review ({max} iterations exhausted)")` and `ctx.status("Failed", f"Max iterations reached ({max})")` and `ctx.log(f"FAILED: Max iterations reached ({max})")` |
| Error/abort (all `return 1` paths) | `ctx.notify(f"dev-loop aborted: {reason}")` and `ctx.status("Error", reason)` and `ctx.log(f"ERROR: {reason}")` |

**Step 5: Log key outcomes (decision results, CI status, worktree path)**

After each significant outcome, add a `ctx.log()`:
- `ctx.log(f"Worktree created at: {worktree_path}")` after worktree setup
- `ctx.log(f"REVIEW {i}/{max}: CI status — {ci_status}")` after CI check
- `ctx.log(f"REVIEW {i}/{max}: Decision — {decision_word}")` after decision gate

**Step 6: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS

**Step 7: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: wire RunContext logging, status, and notifications throughout main loop"
```

---

### Task 5: Pass RunContext to helper functions that need logging

**Files:**
- Modify: `scripts/dev-loop.py` (helper functions that currently print warnings)

**Step 1: Update error-path print statements in helper functions**

The following functions have `print()` calls for warnings/errors that should also go to the log file. Pass `ctx` (or just the log method) and add `ctx.log()` alongside existing prints:

- `gh_comment` (line 101): `ctx.log(f"Warning: failed to post PR comment: {e}")`
- `gh_assign_self` (line 127-129): `ctx.log(f"Assigned {username} to PR #{pr_number}")` and warnings
- `gh_request_review` (line 147-149): `ctx.log(f"Requested review from: {reviewers}")` and warnings
- `wait_for_ci` (line 168, 197, 201, 205, 209): log CI status updates
- `create_worktree_via_claude` (line 254, 267, 278): log worktree discovery

For simplicity, make `ctx` a module-level variable set in `main()` before use, or pass it as a parameter. Module-level is simpler given how many functions need it.

```python
# At module level, after imports:
_ctx: RunContext | None = None

# In main(), after creating RunContext:
global _ctx
_ctx = ctx
```

Then helper functions can call `_ctx.log(...)` when `_ctx is not None`.

**Step 2: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS

**Step 3: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: route helper function warnings through RunContext log"
```

---

### Task 6: Update run_claude to separate stderr

**Files:**
- Modify: `scripts/dev-loop.py:29-45` (run_claude function)

**Step 1: Capture stderr to a separate file**

Currently stderr goes to stdout via `stderr=subprocess.STDOUT`. Instead, write stderr to a `.stderr.log` file alongside the JSON output:

```python
def run_claude(
    prompt: str, output_file: Path, permission_mode: str = "default", cwd: Path | None = None
) -> Path:
    """Run a headless claude session and save output to file."""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if permission_mode != "default":
        cmd += ["--permission-mode", permission_mode]

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    stderr_file = output_file.with_suffix(".stderr.log")
    with open(output_file, "w") as out_f, open(stderr_file, "w") as err_f:
        subprocess.run(cmd, stdout=out_f, stderr=err_f, env=env, cwd=cwd)

    if _ctx is not None:
        _ctx.log(f"Output saved to: {output_file}")
    else:
        print(f"  Output saved to: {output_file}", flush=True)
    return output_file
```

**Step 2: Run quality gates**

Run: `uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py`
Expected: PASS

**Step 3: Commit**

```bash
git add scripts/dev-loop.py
git commit -m "feat: capture stderr separately for each claude session"
```

---

### Task 7: Update commands to mention log directory

**Files:**
- Modify: `commands/dev-loop.md`
- Modify: `commands/review-loop.md`

**Step 1: Update dev-loop.md**

Add after the script description (around line 59-70), mention that logs are written to `.dev-loop/runs/` and can be monitored:

```markdown
Monitor progress from another terminal:
- Status: `watch -n1 cat .dev-loop/latest/status.txt`
- Full log: `tail -f .dev-loop/latest/dev-loop.log`
```

**Step 2: Update review-loop.md similarly**

Add the same monitoring instructions.

**Step 3: Commit**

```bash
git add commands/dev-loop.md commands/review-loop.md
git commit -m "docs: add log monitoring instructions to commands"
```

---

### Task 8: Manual smoke test

**Step 1: Verify directory creation**

Run the script with `--help` to confirm it still parses args, then do a quick dry-run test by checking that `RunContext.__init__` works in a git repo:

```bash
cd /Users/yorrickjansen/work/claude-code-plugins/dev-loop
python3 -c "
import sys; sys.path.insert(0, 'scripts')
# Quick import check — the script uses uv run but we can test the class directly
"
```

Actually, since the script uses `uv run --script`, test via:
```bash
uv run scripts/dev-loop.py --help
```

**Step 2: Verify .dev-loop/ is gitignored**

```bash
git status  # .dev-loop/ should not appear
```

**Step 3: Run quality gates one final time**

```bash
uv run ruff check scripts/dev-loop.py && uv run pyright scripts/dev-loop.py
```

**Step 4: Commit any final fixes**

```bash
git add -A && git commit -m "fix: address quality gate issues from smoke test"
```
