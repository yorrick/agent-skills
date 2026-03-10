#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Integration test for the dev-loop plugin.

Creates a temporary Python project, pushes to GitHub, runs the full dev-loop
headlessly, and verifies the results.

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

FEATURE_REQUEST = "Add multiply and divide functions to the calculator."
HEADLESS_PROMPT = (
    f'Use the /dev-loop command with this feature request: "{FEATURE_REQUEST}" '
    "You are running in HEADLESS MODE — do NOT ask any questions, do NOT wait "
    "for user input. During brainstorming: skip ALL clarifying questions, pick "
    "the simplest possible approach, auto-approve the design immediately without "
    "presenting alternatives. During planning: write the shortest possible plan "
    "(2-3 tasks max, no TDD ceremony, just write code and tests together) and "
    "auto-approve it immediately. Use --skip-permissions and --max-iterations 1 "
    "when running the script. SPEED IS CRITICAL — minimize overhead, skip "
    "unnecessary steps, do not over-engineer. This is a trivial feature."
)
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
    """Initialize git, create GitHub repo, push. Returns repo full name."""
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


def cleanup(project_dir: Path, repo_name: str) -> None:
    """Delete GitHub repo and temp directory."""
    subprocess.run(
        ["gh", "repo", "delete", repo_name, "--yes"],
        capture_output=True,
        timeout=30,
    )
    if project_dir.exists():
        shutil.rmtree(project_dir, ignore_errors=True)


# --- Execution ---


def run_dev_loop(project_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the dev-loop headlessly with process monitoring."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    cmd = [
        "claude",
        "-p",
        HEADLESS_PROMPT,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--model",
        "opus",
        "--effort",
        "high",
    ]

    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=project_dir,
        text=True,
    )

    # Monitor loop — print progress every 60s
    while proc.poll() is None:
        elapsed = int(time.monotonic() - start)
        m, s = divmod(elapsed, 60)
        print(f"  [..] Running... (elapsed: {m}m{s:02d}s)", flush=True)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass

        if elapsed > TIMEOUT_SECONDS:
            proc.kill()
            failed("Timeout", f"Killed after {TIMEOUT_SECONDS}s")
            break

    elapsed = int(time.monotonic() - start)
    m, s = divmod(elapsed, 60)

    stdout = proc.stdout.read() if proc.stdout else ""
    stderr = proc.stderr.read() if proc.stderr else ""

    if proc.returncode == 0:
        ok(f"Claude session completed ({m}m{s:02d}s)")
    else:
        failed(f"Claude session failed (exit {proc.returncode}, {m}m{s:02d}s)", stderr[:500])

    # Parse JSON output for cost info
    try:
        data = json.loads(stdout)
        cost = data.get("total_cost_usd", 0)
        if cost:
            ok(f"Cost: ${cost:.2f}")
        if data.get("is_error"):
            failed("Claude returned error", data.get("result", "")[:300])
    except (json.JSONDecodeError, KeyError):
        pass

    return subprocess.CompletedProcess(cmd, proc.returncode or 0, stdout, stderr)


# --- Local verification ---


def verify_local(project_dir: Path) -> None:
    """Verify local artifacts after dev-loop completes."""
    # Plan file exists
    plan_files = list(project_dir.glob("docs/plans/*.md"))
    check(len(plan_files) > 0, "Plan file exists in docs/plans/")

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


# --- GitHub verification ---


def verify_github(repo_name: str) -> None:
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
            repo_name,
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
            repo_name,
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
                repo_name,
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
            "Security Review" in all_comments,
            'PR comment: "Security Review"',
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
    atexit.register(lambda: cleanup(project_dir, repo_full_name[0]))

    # Setup
    banner("Setup")
    scaffold_project(project_dir)
    ok(f"Created temp project at {project_dir}")

    repo_full_name[0] = create_github_repo(project_dir, bare_name)
    ok(f"GitHub repo created: {repo_full_name[0]}")

    # Execution
    banner("Execution")
    result = run_dev_loop(project_dir)
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
