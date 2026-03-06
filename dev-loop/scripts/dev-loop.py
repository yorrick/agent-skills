#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Automated development loop: implement -> simplify -> review -> fix -> repeat.

Usage:
    dev-loop.py <plan-file> [--max-iterations N] [--pr-url URL] [--skip-permissions]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path


def log(msg: str) -> None:
    print(f"\n{'=' * 64}\n  {msg}\n{'=' * 64}\n", flush=True)


def run_claude(prompt: str, output_file: Path, permission_mode: str = "default") -> Path:
    """Run a headless claude session and save output to file."""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if permission_mode != "default":
        cmd += ["--permission-mode", permission_mode]

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    with open(output_file, "w") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)

    print(f"  Output saved to: {output_file}", flush=True)
    return output_file


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


def run_claude_bg(prompt: str, output_file: Path, permission_mode: str = "default") -> None:
    """Wrapper for ProcessPoolExecutor — must be top-level function."""
    run_claude(prompt, output_file, permission_mode)


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
        found = any(
            v is True for k, v in enabled_plugins.items() if k.startswith(f"{plugin}@")
        )
        if not found:
            missing.append(plugin)

    if missing:
        print(
            "ERROR: dev-loop requires the following plugins "
            "to be installed and enabled:\n"
        )
        for p in missing:
            print(f"  - {p}")
        print("\nInstall missing plugins with:")
        for p in missing:
            print(f"  claude plugin install {p}")
        return False

    return True


def _implementation_prompt(plan_file: Path) -> str:
    return (
        f"Read the plan at {plan_file}. "
        "Use the superpowers:executing-plans skill to implement it task by task.\n\n"
        "After completing all tasks, discover and run the project's quality gates:\n"
        "- Check package.json, Makefile, pyproject.toml, tox.ini, Cargo.toml, or equivalent\n"
        "- Run linting (eslint, ruff, pylint, clippy, etc.)\n"
        "- Run type checking (tsc, mypy, pyright, etc.)\n"
        "- Run formatting check (prettier, black, rustfmt, etc.)\n"
        "- Run the test suite\n\n"
        "Fix any failures before proceeding. Once everything passes, commit all work."
    )


def _pr_creation_prompt(plan_file: Path) -> str:
    return (
        "Push the current branch and create a pull request using gh pr create. "
        "Use a descriptive title and body summarizing what was implemented "
        f"based on the plan at {plan_file}. Return the PR URL."
    )


def _security_review_prompt(pr_url: str) -> str:
    return (
        f"/security-review\n\n"
        f"After completing the security review, post your findings as a comment "
        f"on PR {pr_url} using the gh CLI:\n"
        f"  gh pr comment {extract_pr_number(pr_url)} --body '<your findings>'\n\n"
        f"Format the comment with a '### Security Review' header, "
        f"list any issues found categorized by severity, "
        f"and end with an assessment of whether it's ready to merge."
    )


def _decision_prompt(code_review_text: str, security_review_text: str) -> str:
    return (
        "Based on these review findings, are there Critical or Important "
        "issues that MUST be fixed before merging?\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}\n\n"
        "Answer with EXACTLY one word: YES or NO. "
        "Only answer YES if there are genuinely Critical or Important issues. "
        "Minor suggestions and nitpicks do not count."
    )


def _fix_prompt(pr_url: str, code_review_text: str, security_review_text: str) -> str:
    return (
        f"The following issues were found during review of PR {pr_url}. "
        "Fix all Critical and Important issues. After fixing, run the project's "
        "quality gates (lint, typecheck, format, tests) and make sure everything "
        "passes. Commit and push the fixes.\n\n"
        f"Code Review findings:\n{code_review_text}\n\n"
        f"Security Review findings:\n{security_review_text}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Automated development loop")
    parser.add_argument("plan_file", help="Path to the implementation plan")
    parser.add_argument(
        "--max-iterations", type=int, default=3, help="Max review iterations (default: 3)"
    )
    parser.add_argument(
        "--pr-url", default="", help="Skip implementation, review existing PR"
    )
    parser.add_argument(
        "--skip-permissions", action="store_true", help="Run with bypassPermissions mode"
    )
    args = parser.parse_args()

    plan_file = Path(args.plan_file)
    if not plan_file.exists():
        print(f"Error: plan file not found: {plan_file}", file=sys.stderr)
        return 1

    if not check_dependencies():
        return 1

    permission_mode = "bypassPermissions" if args.skip_permissions else "default"
    pr_url = args.pr_url
    work_dir = Path(tempfile.mkdtemp(prefix="dev-loop-"))
    print(f"Work directory: {work_dir}")

    # --- Phase 1: Implementation (skip if --pr-url provided) ---
    if not pr_url:
        log("Phase 1: Implementing plan")
        run_claude(
            _implementation_prompt(plan_file),
            work_dir / "implementation.json",
            permission_mode,
        )

        log("Phase 1b: Creating PR")
        run_claude(
            _pr_creation_prompt(plan_file),
            work_dir / "pr-creation.json",
            permission_mode,
        )

        pr_url = extract_pr_url(work_dir / "pr-creation.json")
        if not pr_url:
            print(
                f"Error: could not extract PR URL. Check {work_dir / 'pr-creation.json'}",
                file=sys.stderr,
            )
            return 1

        print(f"PR created: {pr_url}")
        gh_comment(pr_url, (
            "### dev-loop: Implementation complete\n\n"
            "Starting automated review loop (simplify + code review + security review).\n\n"
            f"Max iterations: {args.max_iterations}"
        ))

    # --- Phase 2: Review loop ---
    for iteration in range(1, args.max_iterations + 1):
        log(f"Review iteration {iteration} of {args.max_iterations}")
        gh_comment(pr_url, f"### dev-loop: Review iteration {iteration}/{args.max_iterations}")

        # Step 1: Simplify
        log(f"Step 1/{iteration}: Simplify")
        run_claude(
            "/simplify",
            work_dir / f"simplify-{iteration}.json",
            permission_mode,
        )

        run_claude(
            "If there are any uncommitted changes from the simplify pass, "
            "commit them with a descriptive message and push to the current branch.",
            work_dir / f"simplify-commit-{iteration}.json",
            permission_mode,
        )

        # Step 2: Code review + Security review in parallel
        log(f"Step 2/{iteration}: Code review + Security review (parallel)")

        with ProcessPoolExecutor(max_workers=2) as executor:
            code_review_future: Future = executor.submit(
                run_claude_bg,
                f"/code-review:code-review {pr_url}",
                work_dir / f"code-review-{iteration}.json",
                permission_mode,
            )
            security_review_future: Future = executor.submit(
                run_claude_bg,
                _security_review_prompt(pr_url),
                work_dir / f"security-review-{iteration}.json",
                permission_mode,
            )
            code_review_future.result()
            security_review_future.result()

        print(f"  Code review: {work_dir / f'code-review-{iteration}.json'}")
        print(f"  Security review: {work_dir / f'security-review-{iteration}.json'}")

        # Step 3: Decision gate
        code_review_text = extract_result(work_dir / f"code-review-{iteration}.json")
        security_review_text = extract_result(
            work_dir / f"security-review-{iteration}.json"
        )

        log(f"Step 3/{iteration}: Decision gate")
        run_claude(
            _decision_prompt(code_review_text, security_review_text),
            work_dir / f"decision-{iteration}.json",
            permission_mode,
        )

        decision = extract_result(work_dir / f"decision-{iteration}.json")

        if "NO" in decision.upper():
            log("No critical issues found. PR is ready!")
            gh_comment(pr_url, (
                "### dev-loop: Review complete\n\n"
                f"No critical issues found after {iteration} iteration(s). PR is ready for human review."
            ))
            print(f"PR: {pr_url}")
            print(f"Review artifacts: {work_dir}")
            return 0

        # Step 4: Fix issues
        log(f"Step 4/{iteration}: Fixing issues")
        run_claude(
            _fix_prompt(pr_url, code_review_text, security_review_text),
            work_dir / f"fix-{iteration}.json",
            permission_mode,
        )

    log(f"Max iterations ({args.max_iterations}) reached. Review PR manually.")
    gh_comment(pr_url, (
        f"### dev-loop: Max iterations reached ({args.max_iterations})\n\n"
        "There are still outstanding issues. Please review manually."
    ))
    print(f"PR: {pr_url}")
    print(f"Review artifacts: {work_dir}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
