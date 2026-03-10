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
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


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

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
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
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        if "/" in reviewer:
            cmd += ["--add-reviewer", reviewer]
        else:
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


def wait_for_ci(pr_number: str, timeout: int = 600, poll_interval: int = 30) -> tuple[str, str]:
    """Wait for CI checks to complete and return (status, details).

    Returns:
        ("pass", "") if all checks pass or no checks exist.
        ("fail", "<failure details>") if any check fails.
        ("timeout", "") if checks don't complete within timeout.
    """
    # First check if the PR has any checks at all
    result = subprocess.run(
        ["gh", "pr", "checks", pr_number, "--json", "name,state,conclusion"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip() or result.stdout.strip() == "[]":
        print("  No CI checks found, skipping CI wait", flush=True)
        if _ctx:
            _ctx.log("No CI checks found, skipping CI wait")
        return ("pass", "")

    elapsed = 0
    while elapsed < timeout:
        result = subprocess.run(
            ["gh", "pr", "checks", pr_number, "--json", "name,state,conclusion"],
            capture_output=True,
            text=True,
            timeout=30,
        )
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
        time.sleep(poll_interval)
        elapsed += poll_interval

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
            "Error: no PR found for the current branch. "
            "Create a PR first or use the default mode.",
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

    run_claude(prompt, output_file, permission_mode)

    result_text = extract_result(output_file)
    match = re.search(r"WORKTREE_PATH=(.+)", result_text)
    if match:
        worktree_path = Path(match.group(1).strip())
        if worktree_path.exists():
            print(f"  Worktree created at: {worktree_path}", flush=True)
            if _ctx:
                _ctx.log(f"Worktree created at: {worktree_path}")
            return worktree_path

    # Fallback: check git worktree list for our branch
    wt_result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    for line in wt_result.stdout.splitlines():
        if line.startswith("worktree ") and branch_name.replace("/", "-") in line:
            worktree_path = Path(line.split(" ", 1)[1])
            if worktree_path.exists():
                print(f"  Worktree found at: {worktree_path}", flush=True)
                if _ctx:
                    _ctx.log(f"Worktree found at: {worktree_path}")
                return worktree_path

    # Second fallback: look for branch in worktree list
    current_worktree = None
    for line in wt_result.stdout.splitlines():
        if line.startswith("worktree "):
            current_worktree = line.split(" ", 1)[1]
        if line.startswith("branch ") and branch_name in line and current_worktree:
            worktree_path = Path(current_worktree)
            if worktree_path.exists():
                print(f"  Worktree found at: {worktree_path}", flush=True)
                if _ctx:
                    _ctx.log(f"Worktree found at: {worktree_path}")
                return worktree_path

    print(
        f"Error: could not find worktree for branch {branch_name}. Check {output_file}",
        file=sys.stderr,
    )
    sys.exit(1)


def run_claude_bg(
    prompt: str, output_file: Path, permission_mode: str = "default", cwd: str | None = None
) -> None:
    """Wrapper for ProcessPoolExecutor — must be top-level function."""
    run_claude(prompt, output_file, permission_mode, Path(cwd) if cwd else None)


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


def _security_review_prompt(pr_url: str) -> str:
    pr_number = extract_pr_number(pr_url)
    return (
        f"/security-review\n\n"
        f"Review the changes in PR {pr_url}.\n\n"
        "After completing the security review, you MUST post your findings as a comment "
        f"on the PR using the gh CLI:\n"
        f"  gh pr comment {pr_number} --body '<your findings>'\n\n"
        "Format the comment with a '### Security Review' header, "
        "list any issues found categorized by severity, "
        "and end with an assessment of whether it's ready to merge.\n\n"
        "IMPORTANT: Always post a comment with your findings, even if no issues were found, "
        "and even if other review comments already exist on the PR."
    )


def _decision_prompt(code_review_text: str, security_review_text: str, ci_failures: str = "") -> str:
    parts = [
        "Based on these review findings, are there Critical or Important "
        "issues that MUST be fixed before merging?\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}\n\n"
    ]
    if ci_failures:
        parts.append(f"CI/CD failures:\n{ci_failures}\n\n")
    parts.append(
        "Answer with EXACTLY one word: YES or NO. "
        "Only answer YES if there are genuinely Critical or Important issues "
        "OR if CI/CD checks are failing. "
        "Minor suggestions and nitpicks do not count."
    )
    return "".join(parts)


def _fix_prompt(pr_url: str, code_review_text: str, security_review_text: str, ci_failures: str = "") -> str:
    parts = [
        f"The following issues were found during review of PR {pr_url}. "
        "Fix all Critical and Important issues. After fixing, run the project's "
        "quality gates (lint, typecheck, format, tests) and make sure everything "
        "passes. Commit and push the fixes.\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}"
    ]
    if ci_failures:
        parts.append(f"\n\nCI/CD failures (MUST fix):\n{ci_failures}")
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Automated development loop")
    parser.add_argument("issue_url", help="GitHub issue URL containing the implementation plan")
    parser.add_argument("--max-iterations", type=int, default=3, help="Max review iterations (default: 3)")
    parser.add_argument("--review-only", default="", help="Skip implementation, review existing PR")
    parser.add_argument(
        "--continue-pr", action="store_true",
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

    permission_mode = "bypassPermissions" if args.skip_permissions else "default"
    pr_url = args.review_only
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

    # --- Phase 1: Implementation ---
    worktree_path: Path | None = None
    if args.continue_pr:
        # --continue-pr: implement in current directory, push, detect PR, review
        ctx.status("Phase 1", "Implementing plan (continue-pr)")
        ctx.log("PHASE 1: Implementing plan in current directory (continue-pr)")
        impl_file = run_claude(
            _implementation_prompt(issue_url),
            work_dir / "implementation.json",
            permission_mode,
            cwd=None,
        )
        err = check_claude_error(impl_file)
        if err:
            print(f"Error during implementation: {err}", file=sys.stderr)
            ctx.status("Error", "Implementation failed")
            ctx.log(f"ERROR: Implementation failed: {err}")
            ctx.notify("dev-loop aborted: implementation failed")
            return 1

        ctx.status("Phase 1b", "Pushing commits (continue-pr)")
        ctx.log("PHASE 1b: Pushing commits (continue-pr)")
        push_file = run_claude(
            "Push all commits on the current branch to the remote.",
            work_dir / "push.json",
            permission_mode,
            cwd=None,
        )
        err = check_claude_error(push_file)
        if err:
            print(f"Error during push: {err}", file=sys.stderr)
            ctx.status("Error", "Push failed")
            ctx.log(f"ERROR: Push failed: {err}")
            ctx.notify("dev-loop aborted: push failed")
            return 1

        pr_url = detect_pr_url()
        ctx.log(f"Using existing PR: {pr_url}")
        ctx.notify("Implementation complete (continue-pr) -- starting review loop")
        gh_comment(
            pr_url,
            (
                "### dev-loop: Implementation complete (continue-pr)\n\n"
                "Starting automated review loop (simplify + code review + security review).\n\n"
                f"Max iterations: {args.max_iterations}"
            ),
        )

    elif not pr_url:
        ctx.status("Phase 0", "Setting up worktree")
        ctx.log("PHASE 0: Setting up worktree")
        worktree_path = create_worktree_via_claude(issue_url, work_dir / "worktree-setup.json", permission_mode)
        ctx.log(f"Worktree created at: {worktree_path}")

        ctx.status("Phase 1", "Implementing plan")
        ctx.log("PHASE 1: Implementing plan")
        impl_file = run_claude(
            _implementation_prompt(issue_url),
            work_dir / "implementation.json",
            permission_mode,
            cwd=worktree_path,
        )
        err = check_claude_error(impl_file)
        if err:
            print(f"Error during implementation: {err}", file=sys.stderr)
            ctx.status("Error", "Implementation failed")
            ctx.log(f"ERROR: Implementation failed: {err}")
            ctx.notify("dev-loop aborted: implementation failed")
            return 1

        ctx.status("Phase 1b", "Creating PR")
        ctx.log("PHASE 1b: Creating PR")
        pr_file = run_claude(
            _pr_creation_prompt(issue_url),
            work_dir / "pr-creation.json",
            permission_mode,
            cwd=worktree_path,
        )
        err = check_claude_error(pr_file)
        if err:
            print(f"Error during PR creation: {err}", file=sys.stderr)
            ctx.status("Error", "PR creation failed")
            ctx.log(f"ERROR: PR creation failed: {err}")
            ctx.notify("dev-loop aborted: PR creation failed")
            return 1

        pr_url = extract_pr_url(work_dir / "pr-creation.json")
        if not pr_url:
            print(
                f"Error: could not extract PR URL. Check {work_dir / 'pr-creation.json'}",
                file=sys.stderr,
            )
            return 1

        ctx.log(f"PR created: {pr_url}")
        ctx.notify("PR created \u2014 starting review loop")
        gh_assign_self(pr_url)
        gh_comment(
            pr_url,
            (
                "### dev-loop: Implementation complete\n\n"
                "Starting automated review loop (simplify + code review + security review).\n\n"
                f"Max iterations: {args.max_iterations}"
            ),
        )

    # --- Phase 2: Review loop ---
    for iteration in range(1, args.max_iterations + 1):
        ctx.status(f"Review {iteration}/{args.max_iterations}", "Starting")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Starting")
        gh_comment(pr_url, f"### dev-loop: Review iteration {iteration}/{args.max_iterations}")

        # Step 1: Simplify
        ctx.status(f"Review {iteration}/{args.max_iterations}", "Simplify")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Simplify")
        simplify_file = run_claude(
            "/simplify",
            work_dir / f"simplify-{iteration}.json",
            permission_mode,
            cwd=worktree_path,
        )
        err = check_claude_error(simplify_file)
        if err:
            ctx.status("Error", f"Simplify failed: {err}")
            ctx.log(f"ERROR: Simplify failed: {err}")
            ctx.notify("dev-loop aborted: simplify failed")
            gh_comment(pr_url, f"### dev-loop: Aborted\n\nError during simplify step: {err}")
            return 1

        run_claude(
            "If there are any uncommitted changes from the simplify pass, "
            "commit them with a descriptive message and push to the current branch.",
            work_dir / f"simplify-commit-{iteration}.json",
            permission_mode,
            cwd=worktree_path,
        )

        # Step 2: Code review + Security review in parallel
        ctx.status(f"Review {iteration}/{args.max_iterations}", "Code review + Security review (parallel)")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Code review + Security review (parallel)")

        cwd_str = str(worktree_path) if worktree_path else None
        with ProcessPoolExecutor(max_workers=2) as executor:
            code_review_future: Future = executor.submit(
                run_claude_bg,
                f"/code-review:code-review {pr_url}",
                work_dir / f"code-review-{iteration}.json",
                permission_mode,
                cwd_str,
            )
            security_review_future: Future = executor.submit(
                run_claude_bg,
                _security_review_prompt(pr_url),
                work_dir / f"security-review-{iteration}.json",
                permission_mode,
                cwd_str,
            )
            code_review_future.result()
            security_review_future.result()

        ctx.log(f"Code review: {work_dir / f'code-review-{iteration}.json'}")
        ctx.log(f"Security review: {work_dir / f'security-review-{iteration}.json'}")

        # Check for errors in review sessions
        for review_name in ("code-review", "security-review"):
            review_file = work_dir / f"{review_name}-{iteration}.json"
            err = check_claude_error(review_file)
            if err:
                ctx.status("Error", f"{review_name} failed")
                ctx.log(f"ERROR: {review_name} failed: {err}")
                ctx.notify(f"dev-loop aborted: {review_name} failed")
                gh_comment(pr_url, f"### dev-loop: Aborted\n\nError during {review_name}: {err}")
                return 1

        # Step 2b: Wait for CI checks
        ctx.status(f"Review {iteration}/{args.max_iterations}", "Checking CI/CD")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Checking CI/CD")
        pr_number = extract_pr_number(pr_url)
        ci_status, ci_failures = wait_for_ci(pr_number)
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: CI status \u2014 {ci_status}")

        if ci_failures:
            gh_comment(pr_url, f"### dev-loop: CI/CD failures (iteration {iteration})\n\n```\n{ci_failures}\n```")

        # Step 3: Decision gate
        code_review_text = extract_result(work_dir / f"code-review-{iteration}.json")
        security_review_text = extract_result(work_dir / f"security-review-{iteration}.json")

        ctx.status(f"Review {iteration}/{args.max_iterations}", "Decision gate")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Decision gate")

        # CI failure automatically means YES (must fix)
        if ci_status == "fail":
            ctx.log("CI failed \u2014 forcing fix iteration")
            decision = "YES"
        else:
            run_claude(
                _decision_prompt(code_review_text, security_review_text),
                work_dir / f"decision-{iteration}.json",
                permission_mode,
            )
            decision = extract_result(work_dir / f"decision-{iteration}.json")

        decision_label = "YES (issues found)" if "YES" in decision.upper() else "NO (clean)"
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Decision \u2014 {decision_label}")

        if "NO" in decision.upper():
            ctx.status("Done", f"No critical issues after {iteration} iterations")
            ctx.log(f"DONE: PR ready after {iteration} iterations")
            ctx.notify(f"PR ready for review after {iteration} iterations")
            pr_num = extract_pr_number(pr_url)
            if args.reviewers:
                gh_request_review(pr_num, args.reviewers)
            gh_comment(
                pr_url,
                (
                    "### dev-loop: Review complete\n\n"
                    f"No critical issues found after {iteration} iteration(s). "
                    "CI passing. PR is ready for human review."
                ),
            )
            ctx.log(f"PR: {pr_url}")
            ctx.log(f"Review artifacts: {work_dir}")
            return 0

        # Step 4: Fix issues
        ctx.notify(f"Review {iteration}/{args.max_iterations}: Critical issues found, fixing...")
        if ci_status == "fail":
            ctx.notify(f"Review {iteration}/{args.max_iterations}: CI failed, fixing...")
        ctx.status(f"Review {iteration}/{args.max_iterations}", "Fixing issues")
        ctx.log(f"REVIEW {iteration}/{args.max_iterations}: Fixing issues")
        run_claude(
            _fix_prompt(pr_url, code_review_text, security_review_text, ci_failures),
            work_dir / f"fix-{iteration}.json",
            permission_mode,
            cwd=worktree_path,
        )

    ctx.status("Failed", f"Max iterations reached ({args.max_iterations})")
    ctx.log(f"FAILED: Max iterations reached ({args.max_iterations})")
    ctx.notify(f"PR needs manual review ({args.max_iterations} iterations exhausted)")
    pr_num = extract_pr_number(pr_url)
    if args.reviewers:
        gh_request_review(pr_num, args.reviewers)
    gh_comment(
        pr_url,
        (
            f"### dev-loop: Max iterations reached ({args.max_iterations})\n\n"
            "There are still outstanding issues. Please review manually."
        ),
    )
    ctx.log(f"PR: {pr_url}")
    ctx.log(f"Review artifacts: {work_dir}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
