#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Automated development loop: implement -> simplify -> review -> fix -> repeat.

Usage:
    dev-loop.py <issue-url> [--max-iterations N] [--review-only URL] [--continue-pr] [--skip-permissions]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Import engine from same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import END, MaxIterationsExceeded, State, StateGraph, python_node


class RunContext:
    """Manages a per-run directory under .dev-loop/runs/ with status, log, and notification helpers."""

    @staticmethod
    def _git_root() -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
        return Path.cwd()

    def __init__(self) -> None:
        repo_root = self._git_root()
        self._start = time.monotonic()

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d-%H%M%S")
        self._dir = repo_root / ".dev-loop" / "runs" / timestamp
        self._dir.mkdir(parents=True, exist_ok=True)

        # Maintain a 'latest' symlink
        latest = repo_root / ".dev-loop" / "latest"
        latest.unlink(missing_ok=True)
        latest.symlink_to(self._dir)

        # Ensure .dev-loop/ is in .gitignore
        gitignore = repo_root / ".gitignore"
        marker = ".dev-loop/"
        if gitignore.exists():
            content = gitignore.read_text()
            if marker not in content:
                with open(gitignore, "a") as f:
                    if not content.endswith("\n"):
                        f.write("\n")
                    f.write(f"{marker}\n")
        else:
            gitignore.write_text(f"{marker}\n")

    @property
    def dir(self) -> Path:
        """Return the run directory path."""
        return self._dir

    def _elapsed(self) -> str:
        """Return elapsed time since run start as a human-readable string."""
        seconds = int(time.monotonic() - self._start)
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes:02d}m{secs:02d}s"
        if minutes:
            return f"{minutes}m{secs:02d}s"
        return f"{secs}s"

    def status(self, phase: str, detail: str) -> None:
        """Overwrite status.txt with current phase info and print to stdout."""
        line = f"{phase} | {detail} | {self._elapsed()}"
        (self._dir / "status.txt").write_text(line + "\n")
        print(line, flush=True)

    def log(self, message: str) -> None:
        """Append a timestamped line to dev-loop.log and print to stdout."""
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {message}"
        with open(self._dir / "dev-loop.log", "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def notify(self, message: str) -> None:
        """Send a macOS notification. Silently fails if unavailable."""
        safe = message.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe}" with title "dev-loop"',
                ],
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


_ctx: RunContext | None = None


def run_claude(
    prompt: str,
    output_file: Path,
    permission_mode: str = "default",
    cwd: Path | None = None,
    model: str = "opus",
    effort: str = "high",
) -> Path:
    """Run a headless claude session and save output to file."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model, "--effort", effort]
    if permission_mode != "default":
        cmd += ["--permission-mode", permission_mode]

    # Strip CLAUDECODE (nested session detection) and ANTHROPIC_API_KEY
    # (forces claude to use Max subscription instead of pay-per-use API)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    stderr_file = output_file.with_suffix(".stderr.log")
    with open(output_file, "w") as out_f, open(stderr_file, "w") as err_f:
        subprocess.run(cmd, stdout=out_f, stderr=err_f, env=env, cwd=cwd)

    if _ctx is not None:
        _ctx.log(f"Output saved to: {output_file}")
    else:
        print(f"  Output saved to: {output_file}", flush=True)
    return output_file


def check_claude_error(json_file: Path) -> str | None:
    """Check if a claude session output contains an error. Returns error message or None."""
    try:
        data = json.loads(json_file.read_text())
        if data.get("is_error"):
            return data.get("result", "Unknown error")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def extract_result(json_file: Path) -> str:
    """Extract the result text from a claude JSON output file."""
    try:
        data = json.loads(json_file.read_text())
        return data.get("result", data.get("message", json.dumps(data)))
    except (json.JSONDecodeError, KeyError):
        return json_file.read_text()


def extract_pr_url(json_file: Path) -> str | None:
    """Extract a GitHub PR URL from claude output."""
    text = json_file.read_text()
    match = re.search(r'https://github\.com/[^\s"]+/pull/\d+', text)
    return match.group(0) if match else None


def extract_pr_number(pr_url: str) -> str:
    """Extract PR number from a GitHub PR URL."""
    match = re.search(r"/pull/(\d+)", pr_url)
    return match.group(1) if match else ""


def extract_issue_number(issue_url: str) -> str:
    """Extract issue number from a GitHub issue URL."""
    match = re.search(r"/issues/(\d+)", issue_url)
    return match.group(1) if match else ""


def gh_comment(pr_url: str, body: str) -> None:
    """Post a comment on a GitHub PR."""
    pr_number = extract_pr_number(pr_url)
    if not pr_number:
        print("  Warning: could not extract PR number, skipping comment", flush=True)
        return
    try:
        subprocess.run(
            ["gh", "pr", "comment", pr_number, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  Warning: failed to post PR comment: {e}", flush=True)
        if _ctx:
            _ctx.log(f"Warning: failed to post PR comment: {e}")


def gh_assign_self(pr_url: str) -> None:
    """Assign the current GitHub user to a PR."""
    pr_number = extract_pr_number(pr_url)
    if not pr_number:
        print("  Warning: could not extract PR number, skipping assignment", flush=True)
        return
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        username = result.stdout.strip()
        if not username:
            print("  Warning: could not determine GitHub username, skipping assignment", flush=True)
            return
        subprocess.run(
            ["gh", "pr", "edit", pr_number, "--add-assignee", username],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"  Assigned {username} to PR #{pr_number}", flush=True)
        if _ctx:
            _ctx.log(f"Assigned {username} to PR #{pr_number}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  Warning: failed to assign PR: {e}", flush=True)
        if _ctx:
            _ctx.log(f"Warning: failed to assign PR: {e}")


def gh_request_review(pr_number: str, reviewers: str) -> None:
    """Request review from GitHub users or teams on a PR."""
    if not reviewers:
        return
    cmd = ["gh", "pr", "edit", pr_number]
    for reviewer in reviewers.split(","):
        reviewer = reviewer.strip()
        if not reviewer:
            continue
        cmd += ["--add-reviewer", reviewer]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"  Requested review from: {reviewers}", flush=True)
        if _ctx:
            _ctx.log(f"Requested review from: {reviewers}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  Warning: failed to request review: {e}", flush=True)
        if _ctx:
            _ctx.log(f"Warning: failed to request review: {e}")


async def wait_for_ci(pr_number: str, timeout: int = 600, poll_interval: int = 30) -> tuple[str, str]:
    """Wait for CI checks to complete and return (status, details).

    Returns:
        ("pass", "") if all checks pass or no checks exist.
        ("fail", "<failure details>") if any check fails.
        ("timeout", "") if checks don't complete within timeout.
    """
    deadline = time.monotonic() + timeout
    first_iteration = True
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_number, "--json", "name,state,conclusion"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if first_iteration:
            first_iteration = False
            if result.returncode != 0 or not result.stdout.strip() or result.stdout.strip() == "[]":
                print("  No CI checks found, skipping CI wait", flush=True)
                if _ctx:
                    _ctx.log("No CI checks found, skipping CI wait")
                return ("pass", "")
        if result.returncode != 0:
            print(f"  Warning: failed to check CI status: {result.stderr}", flush=True)
            return ("pass", "")

        try:
            checks = json.loads(result.stdout)
        except json.JSONDecodeError:
            return ("pass", "")

        if not checks:
            return ("pass", "")

        all_complete = all(c.get("state") == "COMPLETED" for c in checks)
        if all_complete:
            failures = [c for c in checks if c.get("conclusion") not in ("SUCCESS", "NEUTRAL", "SKIPPED")]
            if not failures:
                print("  CI checks passed", flush=True)
                if _ctx:
                    _ctx.log("CI checks passed")
                return ("pass", "")

            details = "\n".join(f"- {c['name']}: {c.get('conclusion', 'UNKNOWN')}" for c in failures)
            print(f"  CI checks failed:\n{details}", flush=True)
            if _ctx:
                _ctx.log(f"CI checks failed:\n{details}")
            return ("fail", details)

        pending = [c["name"] for c in checks if c.get("state") != "COMPLETED"]
        names = ", ".join(pending[:3]) + ("..." if len(pending) > 3 else "")
        print(f"  Waiting for CI ({len(pending)} pending: {names})...", flush=True)
        await asyncio.sleep(poll_interval)

    print("  CI check timeout reached", flush=True)
    if _ctx:
        _ctx.log("CI check timeout reached")
    return ("timeout", "")


def fetch_issue_body(issue_url: str) -> str:
    """Fetch the body of a GitHub issue."""
    issue_number = extract_issue_number(issue_url)
    if not issue_number:
        print(f"Error: could not extract issue number from {issue_url}", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["gh", "issue", "view", issue_number, "--json", "body", "--jq", ".body"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        print(f"Error: failed to fetch issue: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def detect_pr_url() -> str:
    """Detect the PR URL for the current branch using gh CLI."""
    result = subprocess.run(
        ["gh", "pr", "view", "--json", "url", "--jq", ".url"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(
            "Error: no PR found for the current branch. Create a PR first or use the default mode.",
            file=sys.stderr,
        )
        sys.exit(1)
    url = result.stdout.strip()
    if _ctx:
        _ctx.log(f"Detected PR: {url}")
    return url


def create_worktree_via_claude(issue_url: str, output_file: Path, permission_mode: str = "default") -> Path:
    """Use Claude with superpowers:using-git-worktrees to create a worktree."""
    issue_number = extract_issue_number(issue_url)
    branch_name = f"dev-loop/issue-{issue_number}"

    prompt = (
        f"Create a git worktree for branch '{branch_name}' using the "
        "superpowers:using-git-worktrees skill. Follow the skill exactly.\n\n"
        "After the worktree is created and verified, output EXACTLY this line as the "
        "very last line of your response:\n"
        "WORKTREE_PATH=<absolute path to the worktree>\n\n"
        "Do NOT use superpowers:finishing-a-development-branch — just create the worktree."
    )

    run_claude(prompt, output_file, permission_mode, model="sonnet", effort="high")

    result_text = extract_result(output_file)
    match = re.search(r"WORKTREE_PATH=(.+)", result_text)
    if match:
        worktree_path = Path(match.group(1).strip())
        if worktree_path.exists():
            print(f"  Worktree created at: {worktree_path}", flush=True)
            if _ctx:
                _ctx.log(f"Worktree created at: {worktree_path}")
            return worktree_path

    # Fallback: scan git worktree list once, matching by path or branch ref
    wt_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    current_worktree: str | None = None
    path_match: Path | None = None
    for line in wt_result.stdout.splitlines():
        if line.startswith("worktree "):
            current_worktree = line.split(" ", 1)[1]
            # Match by path (worktree dirs use dashes instead of slashes)
            if branch_name.replace("/", "-") in current_worktree:
                path_match = Path(current_worktree)
        elif line.startswith("branch ") and branch_name in line and current_worktree:
            # Match by branch ref
            worktree_path = Path(current_worktree)
            if worktree_path.exists():
                print(f"  Worktree found at: {worktree_path}", flush=True)
                if _ctx:
                    _ctx.log(f"Worktree found at: {worktree_path}")
                return worktree_path
    if path_match and path_match.exists():
        print(f"  Worktree found at: {path_match}", flush=True)
        if _ctx:
            _ctx.log(f"Worktree found at: {path_match}")
        return path_match

    print(
        f"Error: could not find worktree for branch {branch_name}. Check {output_file}",
        file=sys.stderr,
    )
    sys.exit(1)


def check_dependencies() -> bool:
    """Check that required plugins are installed and enabled."""
    settings_file = Path.home() / ".claude" / "settings.json"
    if not settings_file.exists():
        print("ERROR: Claude Code settings not found", file=sys.stderr)
        return False

    settings = json.loads(settings_file.read_text())
    enabled_plugins = settings.get("enabledPlugins", {})

    required = ["superpowers", "code-review", "code-simplifier"]
    missing = []

    for plugin in required:
        found = any(v is True for k, v in enabled_plugins.items() if k.startswith(f"{plugin}@"))
        if not found:
            missing.append(plugin)

    if missing:
        print("ERROR: dev-loop requires the following plugins to be installed and enabled:\n")
        for p in missing:
            print(f"  - {p}")
        print("\nInstall missing plugins with:")
        for p in missing:
            print(f"  claude plugin install {p}")
        return False

    return True


def _implementation_prompt(issue_url: str) -> str:
    return (
        f"Fetch the implementation plan from GitHub issue {issue_url} using: "
        f"gh issue view {extract_issue_number(issue_url)} --json body --jq .body\n\n"
        "Use the superpowers:executing-plans skill to implement the plan task by task. "
        "Do NOT use superpowers:using-git-worktrees — the branch and worktree are already set up. "
        "Do NOT use superpowers:finishing-a-development-branch — this is a headless session, "
        "do not present interactive options. Just implement, verify, and commit.\n\n"
        "After completing all tasks:\n\n"
        "1. Update documentation to reflect the changes:\n"
        "   - Update README.md if the feature adds new commands, APIs, config options, or usage patterns\n"
        "   - Add or update docstrings/comments for new public functions, classes, and modules\n"
        "   - Update CHANGELOG.md if one exists (add entry under Unreleased)\n"
        "   - Create or update Mermaid diagrams in docs/ for:\n"
        "     - Architecture diagrams if new components or services were added\n"
        "     - Sequence diagrams if new workflows or API flows were introduced\n"
        "     - Data flow diagrams if data pipelines or processing changed\n"
        "   - Use ```mermaid code blocks in markdown files for diagrams\n"
        "   - Only create diagrams that add genuine value — skip if the change is trivial\n\n"
        "2. Discover and run the project's quality gates:\n"
        "   - Check package.json, Makefile, pyproject.toml, tox.ini, Cargo.toml, or equivalent\n"
        "   - Run linting (eslint, ruff, pylint, clippy, etc.)\n"
        "   - Run type checking (tsc, mypy, pyright, etc.)\n"
        "   - Run formatting check (prettier, black, rustfmt, etc.)\n"
        "   - Run the test suite\n\n"
        "Fix any failures before proceeding. Once everything passes, commit all work. "
        "Do NOT push — just commit locally."
    )


def _pr_creation_prompt(issue_url: str) -> str:
    issue_number = extract_issue_number(issue_url)
    return (
        "Push the current branch and create a pull request using gh pr create. "
        "Use a descriptive title and body summarizing what was implemented "
        f"based on the plan in issue #{issue_number}. "
        f"Link the PR to the issue by including 'Closes #{issue_number}' in the body. "
        "Return the PR URL."
    )


def _security_review_prompt(pr_url: str, previous_findings: str = "") -> str:
    pr_number = extract_pr_number(pr_url)
    parts = [f"/security-review\n\nReview the changes in PR {pr_url}.\n\n"]
    if previous_findings:
        parts.append(
            "IMPORTANT: A previous security review iteration found the following issues. "
            "You must do TWO things:\n"
            "1. Check whether each previous issue has been resolved in the current code. "
            "If any remain unresolved, include them in your findings.\n"
            "2. Perform a FULL security review of the current code — the fixes themselves "
            "may have introduced NEW security issues that were not present before. "
            "Do not limit your review to only the previously reported issues.\n\n"
            f"Previous security review findings:\n{previous_findings}\n\n"
        )
    parts.append(
        "After completing the security review, you MUST post your findings as a comment "
        f"on the PR using the gh CLI:\n"
        f"  gh pr comment {pr_number} --body '<your findings>'\n\n"
        "Format the comment with a '### Security Review' header, "
        "list any issues found categorized by severity, "
        "and end with an assessment of whether it's ready to merge.\n\n"
        "IMPORTANT: Always post a comment with your findings, even if no issues were found, "
        "and even if other review comments already exist on the PR."
    )
    return "".join(parts)


def _decision_prompt(code_review_text: str, security_review_text: str, ci_failures: str = "") -> str:
    parts = [
        "Based on these review findings, are there Critical, Important, or Medium "
        "severity issues that MUST be fixed before merging?\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}\n\n"
    ]
    if ci_failures:
        parts.append(f"CI/CD failures:\n{ci_failures}\n\n")
    parts.append(
        "Answer with EXACTLY one word: YES or NO. "
        "Only answer YES if there are genuinely Critical, Important, or Medium severity issues "
        "OR if CI/CD checks are failing. "
        "Low severity suggestions and nitpicks do not count."
    )
    return "".join(parts)


def _smoke_test_prompt(issue_url: str) -> str:
    issue_number = extract_issue_number(issue_url)
    return (
        f"Run a smoke test to verify the implementation actually works.\n\n"
        f"1. Fetch the implementation plan from GitHub issue {issue_url} using:\n"
        f"   gh issue view {issue_number} --json body --jq .body\n\n"
        "2. Look for a 'Validation' section (## Validation header) in the plan.\n\n"
        "3. If a Validation section exists, execute those validation instructions exactly:\n"
        "   - Start any long-running processes (servers, etc.) in the background\n"
        "   - Use a non-standard port (e.g., 8099) to avoid conflicts\n"
        "   - Wait up to 30 seconds for the service to be ready (poll with curl or similar)\n"
        "   - Run each specified check\n"
        "   - ALWAYS kill all background processes before exiting, even on failure\n"
        "   - Report pass/fail for each check\n\n"
        "4. If NO Validation section exists, fall back to convention-based discovery:\n"
        "   - Read README.md, CLAUDE.md, pyproject.toml, package.json, Makefile, docker-compose.yml\n"
        "   - Figure out how to run the application locally\n"
        "   - Perform a basic sanity check: does it start? Does --help work? "
        "Does a health endpoint respond?\n"
        "   - ALWAYS kill all background processes before exiting\n\n"
        "5. End your response with EXACTLY one of these lines (no extra text after it):\n"
        "   SMOKE_TEST_PASS\n"
        "   SMOKE_TEST_FAIL: <brief summary of what failed>\n\n"
        "IMPORTANT: You MUST clean up all background processes before finishing. "
        "Use 'kill %1' or 'kill $PID' to stop any servers you started."
    )


def _smoke_test_fix_prompt(issue_url: str, smoke_test_output: str) -> str:
    issue_number = extract_issue_number(issue_url)
    return (
        "The smoke test failed. Fix the code so the application works correctly.\n\n"
        f"Smoke test output:\n{smoke_test_output}\n\n"
        f"For context, fetch the implementation plan from GitHub issue {issue_url} using:\n"
        f"  gh issue view {issue_number} --json body --jq .body\n\n"
        "Diagnose the root cause from the error output above, fix the code, "
        "then run the project's quality gates (lint, typecheck, format, tests) "
        "to make sure your fixes don't break anything.\n\n"
        "Commit the fixes locally. Do NOT push."
    )


def _fix_prompt(
    pr_url: str,
    code_review_text: str,
    security_review_text: str,
    issue_url: str | None = None,
    ci_failures: str = "",
) -> str:
    parts = [
        f"The following issues were found during review of PR {pr_url}. "
        "Fix all Critical, Important, and Medium severity issues. After fixing, run the project's "
        "quality gates (lint, typecheck, format, tests) and make sure everything "
        "passes. Commit and push the fixes.\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}"
    ]
    if ci_failures:
        parts.append(f"\n\nCI/CD failures (MUST fix):\n{ci_failures}")
    if issue_url:
        issue_number = extract_issue_number(issue_url)
        parts.append(
            "\n\nAfter fixing all issues and running quality gates, re-run the smoke test validation. "
            f"Fetch the plan from issue {issue_url} using:\n"
            f"  gh issue view {issue_number} --json body --jq .body\n\n"
            "Look for the Validation section. Execute the validation checks. "
            "If any long-running processes are needed (servers, etc.), "
            "start them in the background on a non-standard port (e.g., 8099), "
            "run the checks, and kill them before finishing. "
            "If smoke test checks fail, fix those too before committing."
        )
    return "".join(parts)


# --- Graph node wrappers ---
# All nodes use python_node() wrappers around the existing run_claude() function
# to preserve file artifact writing (.dev-loop/runs/) and the integration test contract.


def _get_cwd(state: State) -> Path | None:
    return Path(state["cwd"]) if state.get("cwd") else None


def _worktree_setup_node(state: State) -> State:
    """Set up a git worktree for the feature branch."""
    worktree_path = create_worktree_via_claude(
        state["issue_url"],
        Path(state["work_dir"]) / "worktree-setup.json",
        state.get("permission_mode", "default"),
    )
    return {"worktree_path": str(worktree_path), "cwd": str(worktree_path)}


def _implement_node(state: State) -> State:
    """Run implementation via Claude."""
    cwd = _get_cwd(state)
    impl_file = run_claude(
        _implementation_prompt(state["issue_url"]),
        Path(state["work_dir"]) / "implementation.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="opus",
        effort="high",
    )
    err = check_claude_error(impl_file)
    if err:
        raise RuntimeError(f"Implementation failed: {err}")
    return {"implementation_output": extract_result(impl_file)}


def _run_smoke_test(state: State, output_filename: str) -> tuple[str, str]:
    """Run smoke test and return (result, error)."""
    cwd = _get_cwd(state)
    smoke_file = run_claude(
        _smoke_test_prompt(state["issue_url"]),
        Path(state["work_dir"]) / output_filename,
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="opus",
        effort="high",
    )
    err = check_claude_error(smoke_file)
    smoke_result = extract_result(smoke_file) if not err else ""
    return smoke_result, err or ""


def _smoke_test_node(state: State) -> State:
    """Run smoke test."""
    smoke_result, err = _run_smoke_test(state, "smoke-test.json")
    return {"smoke_test_output": smoke_result, "smoke_test_error": err}


def _smoke_test_fix_node(state: State) -> State:
    """Fix smoke test failures."""
    cwd = _get_cwd(state)
    run_claude(
        _smoke_test_fix_prompt(
            state["issue_url"], state.get("smoke_test_output", "") or state.get("smoke_test_error", "")
        ),
        Path(state["work_dir"]) / "smoke-test-fix.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="opus",
        effort="high",
    )
    return {}


def _smoke_test_retry_node(state: State) -> State:
    """Re-run smoke test after fix."""
    smoke_result, err = _run_smoke_test(state, "smoke-test-retry.json")
    if err or "SMOKE_TEST_FAIL" in smoke_result:
        if _ctx:
            _ctx.status("Error", "Smoke test failed after fix attempt")
            _ctx.log("ERROR: Smoke test still failing after fix attempt")
            _ctx.notify("dev-loop aborted: smoke test failed after fix attempt")
        return {"smoke_test_retry_output": smoke_result, "smoke_test_retry_failed": "true"}
    return {"smoke_test_retry_output": smoke_result}


def _create_pr_node(state: State) -> State:
    """Create PR and assign self."""
    cwd = _get_cwd(state)
    pr_file = run_claude(
        _pr_creation_prompt(state["issue_url"]),
        Path(state["work_dir"]) / "pr-creation.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="sonnet",
        effort="low",
    )
    err = check_claude_error(pr_file)
    if err:
        raise RuntimeError(f"PR creation failed: {err}")

    pr_url = extract_pr_url(Path(state["work_dir"]) / "pr-creation.json")
    if not pr_url:
        raise RuntimeError(f"Could not extract PR URL. Check {Path(state['work_dir']) / 'pr-creation.json'}")

    if _ctx:
        _ctx.log(f"PR created: {pr_url}")
        _ctx.notify("PR created — starting review loop")
    gh_assign_self(pr_url)
    gh_comment(
        pr_url,
        (
            "### dev-loop: Implementation complete\n\n"
            "Starting automated review loop (simplify + code review + security review).\n\n"
            f"Max iterations: {state.get('max_iterations', '5')}"
        ),
    )
    return {"pr_url": pr_url}


def _continue_pr_push_node(state: State) -> State:
    """Push commits and detect existing PR for --continue-pr mode."""
    cwd = _get_cwd(state)
    push_file = run_claude(
        "Push all commits on the current branch to the remote.",
        Path(state["work_dir"]) / "push.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="sonnet",
        effort="low",
    )
    err = check_claude_error(push_file)
    if err:
        raise RuntimeError(f"Push failed: {err}")

    pr_url = detect_pr_url()
    if _ctx:
        _ctx.log(f"Using existing PR: {pr_url}")
        _ctx.notify("Implementation complete (continue-pr) -- starting review loop")
    gh_comment(
        pr_url,
        (
            "### dev-loop: Implementation complete (continue-pr)\n\n"
            "Starting automated review loop (simplify + code review + security review).\n\n"
            f"Max iterations: {state.get('max_iterations', '5')}"
        ),
    )
    return {"pr_url": pr_url}


def _simplify_node(state: State) -> State:
    """Run simplify pass."""
    cwd = _get_cwd(state)
    iteration = int(state.get("iteration_count", "1"))
    max_iterations = state.get("max_iterations", "5")
    pr_url = state.get("pr_url", "")
    if pr_url:
        gh_comment(pr_url, f"### dev-loop: Review iteration {iteration}/{max_iterations}")
    simplify_file = run_claude(
        "/simplify",
        Path(state["work_dir"]) / f"simplify-{iteration}.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="sonnet",
        effort="high",
    )
    err = check_claude_error(simplify_file)
    if err:
        raise RuntimeError(f"Simplify failed: {err}")
    return {}


def _simplify_commit_node(state: State) -> State:
    """Commit and push simplify changes."""
    cwd = _get_cwd(state)
    iteration = int(state.get("iteration_count", "1"))
    run_claude(
        "If there are any uncommitted changes from the simplify pass, "
        "commit them with a descriptive message and push to the current branch.",
        Path(state["work_dir"]) / f"simplify-commit-{iteration}.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="sonnet",
        effort="low",
    )
    return {}


async def _code_review_node(state: State) -> State:
    """Run code review."""
    cwd = _get_cwd(state)
    iteration = int(state.get("iteration_count", "1"))
    pr_url = state["pr_url"]
    review_file = await asyncio.to_thread(
        run_claude,
        f"/code-review:code-review {pr_url}",
        Path(state["work_dir"]) / f"code-review-{iteration}.json",
        state.get("permission_mode", "default"),
        cwd,
        "opus",
        "high",
    )
    err = check_claude_error(review_file)
    if err:
        raise RuntimeError(f"Code review failed: {err}")
    return {"code_review_output": extract_result(review_file)}


async def _security_review_node(state: State) -> State:
    """Run security review."""
    cwd = _get_cwd(state)
    iteration = int(state.get("iteration_count", "1"))
    pr_url = state["pr_url"]
    previous_findings = state.get("previous_security_findings", "")
    review_file = await asyncio.to_thread(
        run_claude,
        _security_review_prompt(pr_url, previous_findings),
        Path(state["work_dir"]) / f"security-review-{iteration}.json",
        state.get("permission_mode", "default"),
        cwd,
        "opus",
        "high",
    )
    err = check_claude_error(review_file)
    if err:
        raise RuntimeError(f"Security review failed: {err}")
    return {"security_review_output": extract_result(review_file)}


async def _wait_for_ci_node(state: State) -> State:
    """Wait for CI checks to complete."""
    pr_url = state["pr_url"]
    pr_number = extract_pr_number(pr_url)
    ci_status, ci_failures = await wait_for_ci(pr_number)
    iteration = state.get("iteration_count", "1")
    if _ctx:
        _ctx.log(f"REVIEW {iteration}: CI status — {ci_status}")
    if ci_failures:
        gh_comment(pr_url, f"### dev-loop: CI/CD failures (iteration {iteration})\n\n```\n{ci_failures}\n```")
    return {"ci_status": ci_status, "ci_failures": ci_failures}


def _decision_node(state: State) -> State:
    """Evaluate review findings and decide whether to fix or finish."""
    code_review_text = state.get("code_review_output", "")
    security_review_text = state.get("security_review_output", "")
    ci_status = state.get("ci_status", "pass")
    ci_failures = state.get("ci_failures", "")
    iteration = int(state.get("iteration_count", "1"))

    # CI failure automatically means YES (must fix)
    if ci_status == "fail":
        if _ctx:
            _ctx.log("CI failed — forcing fix iteration")
        return {
            "decision_output": "YES",
            "iteration_count": str(iteration),
            "previous_security_findings": security_review_text,
        }

    decision_file = run_claude(
        _decision_prompt(code_review_text, security_review_text, ci_failures),
        Path(state["work_dir"]) / f"decision-{iteration}.json",
        state.get("permission_mode", "default"),
        model="sonnet",
        effort="low",
    )
    decision = extract_result(decision_file)
    decision_label = "YES (issues found)" if "YES" in decision.upper() else "NO (clean)"
    if _ctx:
        _ctx.log(f"REVIEW {iteration}: Decision — {decision_label}")
    return {
        "decision_output": decision,
        "iteration_count": str(iteration),
        "previous_security_findings": security_review_text,
    }


def _fix_node(state: State) -> State:
    """Fix issues found during review."""
    cwd = _get_cwd(state)
    iteration = int(state.get("iteration_count", "1"))
    pr_url = state["pr_url"]
    code_review_text = state.get("code_review_output", "")
    security_review_text = state.get("security_review_output", "")
    ci_failures = state.get("ci_failures", "")
    issue_url = state.get("issue_url")

    run_claude(
        _fix_prompt(pr_url, code_review_text, security_review_text, issue_url=issue_url, ci_failures=ci_failures),
        Path(state["work_dir"]) / f"fix-{iteration}.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="opus",
        effort="high",
    )
    # Increment iteration count for next round
    return {"iteration_count": str(iteration + 1)}


# --- Router functions ---


def _smoke_test_router(state: State) -> str:
    """Route based on smoke test results and mode."""
    smoke_output = state.get("smoke_test_output", "")
    smoke_error = state.get("smoke_test_error", "")
    if smoke_error or "SMOKE_TEST_FAIL" in smoke_output:
        return "fail"
    # In continue-pr mode, go to push instead of create_pr
    if state.get("mode") == "continue_pr":
        return "pass_continue"
    return "pass"


def _post_smoke_test_router(state: State) -> str:
    """Route based on smoke test retry result — abort on persistent failure."""
    if state.get("smoke_test_retry_failed") == "true":
        return "abort"
    if state.get("mode") == "continue_pr":
        return "continue_pr_push"
    return "create_pr"


def _decision_router(state: State) -> str:
    """Route based on decision gate output."""
    decision = state.get("decision_output", "NO")
    if "YES" in decision.upper():
        return "fix"
    return "done"


def _build_graph(max_iterations: int) -> StateGraph:
    """Build the dev-loop workflow graph."""
    graph = StateGraph(max_iterations=max_iterations)

    # Register all nodes
    graph.add_node("worktree_setup", python_node(_worktree_setup_node))
    graph.add_node("implement", python_node(_implement_node))
    graph.add_node("smoke_test", python_node(_smoke_test_node))
    graph.add_node("smoke_test_fix", python_node(_smoke_test_fix_node))
    graph.add_node("smoke_test_retry", python_node(_smoke_test_retry_node))
    graph.add_node("create_pr", python_node(_create_pr_node))
    graph.add_node("continue_pr_push", python_node(_continue_pr_push_node))
    graph.add_node("simplify", python_node(_simplify_node))
    graph.add_node("simplify_commit", python_node(_simplify_commit_node))
    graph.add_node("code_review", python_node(_code_review_node))
    graph.add_node("security_review", python_node(_security_review_node))
    graph.add_node("wait_for_ci", python_node(_wait_for_ci_node))
    graph.add_node("decision", python_node(_decision_node))
    graph.add_node("fix", python_node(_fix_node))

    # Phase 1 edges (default path: worktree -> implement -> smoke_test -> create_pr)
    graph.add_edge("start", "worktree_setup")
    graph.add_edge("worktree_setup", "implement")
    graph.add_edge("implement", "smoke_test")
    graph.add_conditional_edges(
        "smoke_test",
        _smoke_test_router,
        {
            "pass": "create_pr",
            "pass_continue": "continue_pr_push",
            "fail": "smoke_test_fix",
        },
    )
    graph.add_edge("smoke_test_fix", "smoke_test_retry")
    # After retry, proceed to PR node or abort if still failing
    graph.add_conditional_edges(
        "smoke_test_retry",
        _post_smoke_test_router,
        {
            "create_pr": "create_pr",
            "continue_pr_push": "continue_pr_push",
            "abort": END,
        },
    )
    graph.add_edge("create_pr", "simplify")
    graph.add_edge("continue_pr_push", "simplify")

    # Phase 2 edges (review loop)
    graph.add_edge("simplify", "simplify_commit")
    graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
    graph.add_edge("code_review", "wait_for_ci")
    graph.add_edge("security_review", "wait_for_ci")
    graph.add_edge("wait_for_ci", "decision")
    graph.add_conditional_edges("decision", _decision_router, {"fix": "fix", "done": END})
    graph.add_edge("fix", "simplify")

    return graph


def main() -> int:
    parser = argparse.ArgumentParser(description="Automated development loop")
    parser.add_argument("issue_url", help="GitHub issue URL containing the implementation plan")
    parser.add_argument("--max-iterations", type=int, default=5, help="Max review iterations (default: 5)")
    parser.add_argument("--review-only", default="", help="Skip implementation, review existing PR")
    parser.add_argument(
        "--continue-pr",
        action="store_true",
        help="Continue implementing in current directory, push, and review existing PR",
    )
    parser.add_argument("--skip-permissions", action="store_true", help="Run with bypassPermissions mode")
    parser.add_argument(
        "--reviewers", default="", help="Comma-separated GitHub usernames or team slugs to request review from"
    )
    args = parser.parse_args()

    issue_url = args.issue_url
    if not re.match(r"https://github\.com/.+/issues/\d+", issue_url):
        print(f"Error: invalid GitHub issue URL: {issue_url}", file=sys.stderr)
        return 1

    if not check_dependencies():
        return 1

    if args.continue_pr and args.review_only:
        print("Error: --continue-pr and --review-only are mutually exclusive", file=sys.stderr)
        return 1

    if args.continue_pr:
        branch_result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=10)
        current_branch = branch_result.stdout.strip()
        if current_branch in ("main", "master"):
            print(
                f"Error: --continue-pr cannot be used on '{current_branch}'. Check out a feature branch first.",
                file=sys.stderr,
            )
            return 1

    if args.review_only and not re.match(r"https://github\.com/.+/pull/\d+", args.review_only):
        print(f"Error: invalid GitHub PR URL: {args.review_only}", file=sys.stderr)
        return 1

    permission_mode = "bypassPermissions" if args.skip_permissions else "default"
    ctx = RunContext()
    global _ctx
    _ctx = ctx
    work_dir = ctx.dir
    ctx.log(f"START dev-loop for {issue_url}")
    ctx.log(f"Run directory: {work_dir}")
    ctx.log(
        f"Options: max_iterations={args.max_iterations},"
        f" continue_pr={args.continue_pr},"
        f" skip_permissions={args.skip_permissions}, reviewers={args.reviewers}"
    )

    # Build the workflow graph
    graph = _build_graph(args.max_iterations)

    # Wire up observability
    last_known_state: State = {}

    @graph.on_node_start
    async def _on_start(node_name: str, state: State) -> None:
        ctx.status(node_name, "Running")
        ctx.log(f"Starting: {node_name}")

    @graph.on_node_end
    async def _on_end(node_name: str, state: State) -> None:
        ctx.log(f"Finished: {node_name}")
        last_known_state.update(state)

    @graph.on_error
    async def _on_err(node_name: str, error: Exception) -> None:
        ctx.log(f"ERROR in {node_name}: {error}")

    # Build initial state
    initial_state: State = {
        "issue_url": issue_url,
        "work_dir": str(work_dir),
        "permission_mode": permission_mode,
        "max_iterations": str(args.max_iterations),
        "reviewers": args.reviewers,
        "previous_security_findings": "",
        "iteration_count": "1",
    }

    # Determine start node and mode-specific state
    start: str | None = None
    if args.review_only:
        initial_state["pr_url"] = args.review_only
        initial_state["cwd"] = ""  # use current directory
        start = "simplify"
    elif args.continue_pr:
        initial_state["cwd"] = ""  # use current directory
        initial_state["mode"] = "continue_pr"
        start = "implement"

    try:
        result = asyncio.run(graph.run(initial_state, start_node=start))

        # Smoke test abort: graph returned normally but the smoke test failed
        if result.get("smoke_test_retry_failed") == "true":
            ctx.status("Failed", "Smoke test failed — aborting")
            ctx.log("FAILED: Smoke test failed after retry — aborting")
            ctx.notify("dev-loop aborted: smoke test failed after retry")
            return 1

        # Post-success actions
        pr_url = result.get("pr_url", "")
        iterations = result.get("iteration_count", "?")
        if args.reviewers and pr_url:
            gh_request_review(extract_pr_number(pr_url), args.reviewers)
        if pr_url:
            gh_comment(
                pr_url,
                (
                    "### dev-loop: Review complete\n\n"
                    f"No critical issues found after {iterations} iteration(s). "
                    "PR is ready for human review."
                ),
            )
        ctx.status("Done", f"No critical issues after {iterations} iterations")
        ctx.log(f"DONE: PR ready after {iterations} iterations")
        ctx.notify(f"PR ready for review after {iterations} iterations")
        ctx.log(f"PR: {pr_url}")
        return 0
    except MaxIterationsExceeded:
        pr_url = last_known_state.get("pr_url", "")
        if pr_url:
            gh_comment(
                pr_url,
                (
                    "### dev-loop: Max iterations reached\n\n"
                    f"Review loop exhausted {args.max_iterations} iteration(s) "
                    "without resolving all issues. PR needs manual review."
                ),
            )
        ctx.status("Failed", f"Max iterations reached ({args.max_iterations})")
        ctx.log(f"FAILED: Max iterations reached ({args.max_iterations})")
        ctx.notify(f"PR needs manual review ({args.max_iterations} iterations exhausted)")
        return 1
    except Exception as e:
        ctx.status("Error", str(e))
        ctx.log(f"ERROR: {e}")
        ctx.notify(f"dev-loop aborted: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
