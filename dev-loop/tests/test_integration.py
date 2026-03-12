#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Integration test for the dev-loop plugin.

Creates a temporary Python project, pushes to GitHub, creates a plan issue,
runs dev-loop.py directly, and verifies the results.

Usage:
    uv run tests/test_integration.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- Configuration ---

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
DEV_LOOP_SCRIPT = SCRIPT_DIR / "dev-loop.py"

PLAN_CONTENT = """\
# Add Multiply and Divide Functions — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:executing-plans to implement this plan.

**Goal:** Add `multiply` and `divide` functions to the calculator library with tests.

**Architecture:** Two new functions in `src/calculator/core.py` following the existing pattern. Tests in `tests/test_core.py`.

**Tech Stack:** Python 3.10+, pytest

---

### Task 1: Add multiply and divide functions with tests

**Files:**
- Modify: `src/calculator/core.py`
- Modify: `tests/test_core.py`

**Step 1: Add multiply function to core.py**

Add after the `subtract` function:

```python
def multiply(a: float, b: float) -> float:
    \\"\\"\\"Multiply two numbers.\\"\\"\\"
    return a * b
```

**Step 2: Add divide function to core.py**

```python
def divide(a: float, b: float) -> float:
    \\"\\"\\"Divide a by b. Raises ZeroDivisionError if b is 0.\\"\\"\\"
    return a / b
```

**Step 3: Add tests to test_core.py**

```python
from calculator.core import add, subtract, multiply, divide
import pytest


def test_multiply():
    assert multiply(2, 3) == 6
    assert multiply(-1, 5) == -5
    assert multiply(0, 100) == 0


def test_divide():
    assert divide(10, 2) == 5.0
    assert divide(-6, 3) == -2.0
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
```

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/calculator/core.py tests/test_core.py
git commit -m "feat: add multiply and divide functions"
```

## Validation

### Sanity check
- Run: `uv run python -c "from calculator.core import multiply, divide; print(multiply(2, 3))"` — exits 0, prints `6`
- Run: `uv run python -c "from calculator.core import divide; print(divide(10, 2))"` — exits 0, prints `5.0`

### Functional checks
- `uv run pytest tests/ -v` exits 0 with all tests passing
"""

TIMEOUT_SECONDS = 2700  # 45 minutes


# --- Output helpers ---

_failures: list[str] = []
_passes: int = 0


def banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}\n", flush=True)


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def passed(msg: str) -> None:
    global _passes
    _passes += 1
    print(f"  [PASS] {msg}", flush=True)


def failed(msg: str, detail: str = "") -> None:
    _failures.append(msg)
    print(f"  [FAIL] {msg}", flush=True)
    if detail:
        print(f"         {detail}", flush=True)


def check(condition: bool, msg: str, detail: str = "") -> bool:
    if condition:
        passed(msg)
    else:
        failed(msg, detail)
    return condition


# --- Setup ---


def scaffold_project(project_dir: Path) -> None:
    """Create a minimal Python calculator project."""
    (project_dir / "src" / "calculator").mkdir(parents=True)
    (project_dir / "tests").mkdir()
    # Pre-create .worktrees/ so superpowers:using-git-worktrees doesn't ask
    (project_dir / ".worktrees").mkdir()

    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "calculator"\nversion = "0.1.0"\n'
        'requires-python = ">=3.10"\n\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n\n'
        "[tool.ruff]\nline-length = 100\n\n"
        '[tool.pyright]\npythonVersion = "3.10"\n'
    )

    (project_dir / "src" / "calculator" / "__init__.py").write_text('"""A simple calculator library."""\n')

    (project_dir / "src" / "calculator" / "core.py").write_text(
        '"""Core calculator operations."""\n\n\n'
        "def add(a: float, b: float) -> float:\n"
        '    """Add two numbers."""\n'
        "    return a + b\n\n\n"
        "def subtract(a: float, b: float) -> float:\n"
        '    """Subtract b from a."""\n'
        "    return a - b\n"
    )

    (project_dir / "tests" / "__init__.py").write_text("")

    (project_dir / "tests" / "test_core.py").write_text(
        '"""Tests for core calculator operations."""\n\n'
        "from calculator.core import add, subtract\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(-1, 1) == 0\n"
        "    assert add(0, 0) == 0\n\n\n"
        "def test_subtract():\n"
        "    assert subtract(5, 3) == 2\n"
        "    assert subtract(0, 5) == -5\n"
    )


def create_github_repo(project_dir: Path, repo_name: str) -> str:
    """Initialize git, create GitHub repo, push. Returns OWNER/REPO."""
    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: basic calculator"],
        cwd=project_dir,
        capture_output=True,
    )
    result = subprocess.run(
        ["gh", "repo", "create", repo_name, "--private", "--source=.", "--push"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Failed to create GitHub repo: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    # Extract owner/repo from the first line of stdout (URL like https://github.com/owner/repo)
    # gh repo create --push may also print git tracking info on subsequent lines
    repo_url = result.stdout.strip().splitlines()[0].strip()
    parts = repo_url.rstrip("/").split("/")
    return f"{parts[-2]}/{parts[-1]}"


def create_issue_with_plan(repo_full_name: str) -> str:
    """Create a GitHub issue with the implementation plan. Returns issue URL."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            repo_full_name,
            "--title",
            "Add multiply and divide functions",
            "--body",
            PLAN_CONTENT,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Failed to create issue: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def cleanup(project_dir: Path, repo_full_name: str) -> None:
    """Delete GitHub repo and temp directory."""
    subprocess.run(
        ["gh", "repo", "delete", repo_full_name, "--yes"],
        capture_output=True,
        timeout=30,
    )
    # Clean up any worktrees before removing the directory
    if project_dir.exists():
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        for line in wt_result.stdout.splitlines():
            if line.startswith("worktree ") and str(project_dir) not in line:
                wt_path = line.split(" ", 1)[1]
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt_path],
                    cwd=project_dir,
                    capture_output=True,
                )
                if Path(wt_path).exists():
                    shutil.rmtree(wt_path, ignore_errors=True)
        shutil.rmtree(project_dir, ignore_errors=True)


# --- Execution ---


def run_dev_loop_script(project_dir: Path, issue_url: str) -> subprocess.CompletedProcess[str]:
    """Run dev-loop.py directly with process monitoring."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    cmd = [
        "uv",
        "run",
        str(DEV_LOOP_SCRIPT),
        issue_url,
        "--skip-permissions",
        "--max-iterations",
        "2",
    ]

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=project_dir,
        text=True,
    )

    output_lines: list[str] = []

    # Monitor loop — print progress every 60s
    while proc.poll() is None:
        elapsed = int(time.monotonic() - start)
        m, s = divmod(elapsed, 60)
        print(f"  [..] Running... (elapsed: {m}m{s:02d}s)", flush=True)

        # Read any available output
        if proc.stdout:
            import select

            while select.select([proc.stdout], [], [], 0)[0]:
                line = proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line)
                print(f"       {line.rstrip()}", flush=True)

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass

        if elapsed > TIMEOUT_SECONDS:
            proc.kill()
            failed("Timeout", f"Killed after {TIMEOUT_SECONDS}s")
            break

    # Read remaining output
    if proc.stdout:
        for line in proc.stdout:
            output_lines.append(line)

    elapsed = int(time.monotonic() - start)
    m, s = divmod(elapsed, 60)

    stdout = "".join(output_lines)

    if proc.returncode == 0:
        ok(f"dev-loop.py completed ({m}m{s:02d}s)")
    else:
        failed(
            f"dev-loop.py failed (exit {proc.returncode}, {m}m{s:02d}s)",
            stdout[-500:] if stdout else "",
        )

    return subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout, "")


# --- Local verification ---


def verify_local(project_dir: Path) -> None:
    """Verify local artifacts after dev-loop completes."""
    # Find worktree or feature branch
    wt_result = subprocess.run(
        ["git", "worktree", "list"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    has_worktree = "dev-loop/" in wt_result.stdout
    check(has_worktree, "Worktree created for feature branch")

    # Find the worktree path to check implementation
    worktree_dir = None
    for line in wt_result.stdout.splitlines():
        if "dev-loop/" in line:
            worktree_dir = Path(line.split()[0])
            break

    if worktree_dir is None:
        # Fallback: check main project dir
        worktree_dir = project_dir

    # Implementation files
    core_py = worktree_dir / "src" / "calculator" / "core.py"
    if core_py.exists():
        content = core_py.read_text()
        check("def multiply" in content, "multiply function exists in core.py")
        check("def divide" in content, "divide function exists in core.py")
    else:
        failed("core.py not found", str(core_py))

    # Tests exist and pass
    test_files = list(worktree_dir.glob("tests/test_*.py"))
    check(len(test_files) > 0, "Test files exist")

    test_result = subprocess.run(
        ["uv", "run", "pytest", "-x", "-q"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        test_result.returncode == 0,
        "Tests pass",
        test_result.stdout[-200:] if test_result.returncode != 0 else "",
    )

    # Git commits on feature branch
    log_result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )
    commit_count = len(log_result.stdout.strip().splitlines())
    check(commit_count > 1, f"Feature branch has commits ({commit_count} total)")

    # Smoke test was executed
    dev_loop_dir = project_dir / ".dev-loop"
    if dev_loop_dir.exists():
        run_dirs = sorted((dev_loop_dir / "runs").iterdir()) if (dev_loop_dir / "runs").exists() else []
        if run_dirs:
            latest_run = run_dirs[-1]
            smoke_json = latest_run / "smoke-test.json"
            check(smoke_json.exists(), "Smoke test was executed (smoke-test.json exists)")

            # Check log mentions smoke test phase
            log_file = latest_run / "dev-loop.log"
            if log_file.exists():
                log_content = log_file.read_text()
                check(
                    "Starting: smoke_test" in log_content or "PHASE 1.5: Smoke test" in log_content,
                    "Log contains smoke test phase",
                )
            else:
                failed("dev-loop.log not found")
        else:
            failed("No run directories found in .dev-loop/runs/")
    else:
        failed(".dev-loop directory not found")


# --- GitHub verification ---


def verify_github(repo_full_name: str) -> None:
    """Verify GitHub state after dev-loop completes."""

    def gh_json(cmd: list[str]) -> dict | list:  # type: ignore[type-arg]
        result = subprocess.run(
            ["gh", *cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        try:
            return json.loads(result.stdout)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return {}

    # Issue exists
    issues = gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo_full_name,
            "--json",
            "number,title,body",
            "--limit",
            "5",
        ]
    )
    check(
        isinstance(issues, list) and len(issues) > 0,
        "Issue exists",
    )

    if isinstance(issues, list) and issues:
        body = issues[0].get("body", "")
        check(len(body) > 50, "Issue has plan in body")

    # PR exists
    prs = gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo_full_name,
            "--state",
            "all",
            "--json",
            "number,title,state,baseRefName",
        ]
    )
    check(
        isinstance(prs, list) and len(prs) > 0,
        "PR exists",
    )

    if isinstance(prs, list) and prs:
        pr = prs[0]
        pr_number = str(pr.get("number", ""))
        check(pr.get("baseRefName") == "main", "PR targets main")

        # PR comments
        comments = gh_json(
            [
                "pr",
                "view",
                pr_number,
                "--repo",
                repo_full_name,
                "--json",
                "comments",
            ]
        )
        comment_bodies: list[str] = []
        if isinstance(comments, dict):
            comment_bodies = [c.get("body", "") for c in comments.get("comments", []) if isinstance(c, dict)]
        all_comments = "\n".join(comment_bodies)

        check(
            "Implementation complete" in all_comments,
            'PR comment: "Implementation complete"',
        )
        check(
            "Review iteration" in all_comments,
            'PR comment: "Review iteration"',
        )
        check(
            "Review complete" in all_comments or "Max iterations" in all_comments,
            "PR comment: final status",
        )


# --- Main ---


def main() -> int:
    banner("dev-loop integration test")

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    bare_name = f"dev-loop-integration-test-{timestamp}"
    project_dir = Path(tempfile.mkdtemp(prefix="dev-loop-integration-test-"))

    # Will be set to OWNER/REPO after creation; cleanup uses whatever value is current
    repo_full_name: list[str] = [bare_name]
    do_cleanup = "--no-cleanup" not in sys.argv
    if do_cleanup:
        atexit.register(lambda: cleanup(project_dir, repo_full_name[0]))

    # Setup
    banner("Setup")
    scaffold_project(project_dir)
    ok(f"Created temp project at {project_dir}")

    repo_full_name[0] = create_github_repo(project_dir, bare_name)
    ok(f"GitHub repo created: {repo_full_name[0]}")

    issue_url = create_issue_with_plan(repo_full_name[0])
    ok(f"Issue created: {issue_url}")

    # Execution — run dev-loop.py directly (skips brainstorming/planning)
    banner("Execution")
    result = run_dev_loop_script(project_dir, issue_url)
    _ = result  # Used for debugging if needed

    # Verification
    banner("Local verification")
    verify_local(project_dir)

    banner("GitHub verification")
    verify_github(repo_full_name[0])

    # Summary
    total = _passes + len(_failures)
    banner(f"RESULT: {_passes}/{total} passed, {len(_failures)} failed")
    if _failures:
        for f in _failures:
            print(f"  - {f}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
