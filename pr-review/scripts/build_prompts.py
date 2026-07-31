#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Build review prompts from templates and PR metadata.

Generates per-provider review and security prompts (claude, gemini, codex)
from their respective templates.

Usage:
    build_prompts.py <pr-number> [--repo OWNER/REPO] [--out-dir DIR]

Outputs:
    <out-dir>/pr-review-prompt-claude-<PR>.md
    <out-dir>/pr-review-prompt-gemini-<PR>.md
    <out-dir>/pr-review-prompt-codex-<PR>.md
    <out-dir>/pr-review-security-claude-<PR>.md
    <out-dir>/pr-review-security-gemini-<PR>.md
    <out-dir>/pr-review-security-codex-<PR>.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_gh(args: list[str]) -> str:
    """Run a gh CLI command and return stdout."""
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"gh {' '.join(args)} failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def get_pr_metadata(pr_number: str, repo: str | None = None) -> dict:
    """Fetch PR metadata via gh CLI."""
    args = ["pr", "view", pr_number, "--json", "number,title,headRefName,baseRefName,headRefOid,url,body"]
    if repo:
        args.extend(["--repo", repo])
    return json.loads(run_gh(args))


def get_repo_name() -> str:
    """Get the owner/repo for the current directory."""
    return run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])


def fill_template(template: str, variables: dict[str, str]) -> str:
    """Replace {{KEY}} placeholders in a template."""
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PR review prompts")
    parser.add_argument("pr_number", help="PR number to review")
    parser.add_argument("--repo", help="Owner/repo (auto-detected if omitted)")
    parser.add_argument("--out-dir", default="/tmp", help="Output directory")
    args = parser.parse_args()

    # Resolve paths
    skill_dir = Path(__file__).resolve().parent.parent / "skills" / "pr-review"
    refs_dir = skill_dir / "references"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Fetch PR metadata
    pr = get_pr_metadata(args.pr_number, args.repo)
    repo = args.repo or get_repo_name()

    variables = {
        "PR_URL": pr["url"],
        "PR_NUMBER": str(pr["number"]),
        "PR_TITLE": pr["title"],
        "PR_BODY": pr.get("body", ""),
        "HEAD_BRANCH": pr["headRefName"],
        "BASE_BRANCH": pr["baseRefName"],
        "COMMIT_SHA": pr["headRefOid"],
        "REPO": repo,
    }

    # Per-provider review and security prompts
    prompts = {}
    for prompt_type in ("review-prompt", "security-prompt"):
        for provider in ("claude", "gemini", "codex"):
            template_path = refs_dir / f"{prompt_type}-{provider}.md"
            if template_path.exists():
                template = template_path.read_text()
                filled = fill_template(template, variables)
                key = prompt_type.replace("-", "_")
                out_path = out_dir / f"pr-{prompt_type}-{provider}-{args.pr_number}.md"
                out_path.write_text(filled)
                prompts[f"{key}_{provider}"] = str(out_path)

    # Output metadata as JSON for the skill to consume
    output = {
        "pr_number": pr["number"],
        "pr_title": pr["title"],
        "pr_url": pr["url"],
        "repo": repo,
        "commit_sha": pr["headRefOid"],
        **prompts,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
