#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Post approved findings as a GitHub PR review with line-level comments.

Usage:
    post_review.py <approved-findings-json> --repo OWNER/REPO --pr NUMBER --commit-sha SHA

Reads a JSON file of approved findings and creates a single atomic
GitHub review with individual line-level comments. Findings that
reference lines outside the diff are included in the review body
instead of as inline comments (GitHub rejects inline comments on
lines that weren't changed).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def get_diff_lines(pr_number: str, repo: str | None = None) -> dict[str, set[int]]:
    """Parse the PR diff to find which lines are actually in the diff.

    Returns a dict of {file_path: set of line numbers on the RIGHT side}.
    """
    args = ["pr", "diff", pr_number]
    if repo:
        args.extend(["--repo", repo])

    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        print(f"Warning: could not fetch diff: {result.stderr}", file=sys.stderr)
        return {}

    diff_lines: dict[str, set[int]] = {}
    current_file = None
    right_line = 0

    for line in result.stdout.splitlines():
        if line.startswith("diff --git"):
            # Extract b/ path
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else None
            if current_file:
                diff_lines.setdefault(current_file, set())
        elif line.startswith("@@") and current_file:
            # Parse hunk header: @@ -old,count +new,count @@
            try:
                plus_part = line.split("+")[1].split("@@")[0].strip()
                right_line = int(plus_part.split(",")[0]) - 1
            except (IndexError, ValueError):
                pass
        elif current_file and not line.startswith("---") and not line.startswith("+++"):
            if line.startswith("-"):
                pass  # deleted line, doesn't exist on RIGHT side
            elif line.startswith("+"):
                right_line += 1
                diff_lines[current_file].add(right_line)
            else:
                right_line += 1
                # Context lines are in the diff too — GitHub accepts comments on them

    return diff_lines


def build_comment_body(finding: dict) -> str:
    """Build the markdown body for a single finding comment."""
    parts = [finding["description"]]
    if finding.get("suggestion"):
        parts.append(f"\n**Suggestion:** {finding['suggestion']}")
    providers = ", ".join(finding.get("providers", ["unknown"]))
    confidence = finding.get("confidence", "?")
    parts.append(f"\n---\n*Found by: {providers} | Confidence: {confidence} | Human-vetted*")
    return "\n".join(parts)


def post_review(
    repo: str,
    pr_number: str,
    commit_sha: str,
    findings: list[dict],
) -> str:
    """Post a review to GitHub with line-level comments.

    Findings on lines outside the diff are included in the review body
    instead of as inline comments.
    """
    diff_lines = get_diff_lines(pr_number, repo)

    inline_comments = []
    body_only_findings = []

    for f in findings:
        file_path = f["file"]
        line = f["line"]

        # Check if this line is in the diff
        file_diff = diff_lines.get(file_path, set())
        if file_diff and line in file_diff:
            inline_comments.append({
                "path": file_path,
                "line": line,
                "side": "RIGHT",
                "body": build_comment_body(f),
            })
        elif file_diff:
            # File is in diff but this specific line isn't — find nearest diff line
            nearest = min(file_diff, key=lambda l: abs(l - line))
            comment_body = build_comment_body(f)
            comment_body = f"*(Note: original finding was on line {line})*\n\n{comment_body}"
            inline_comments.append({
                "path": file_path,
                "line": nearest,
                "side": "RIGHT",
                "body": comment_body,
            })
        else:
            # File not in diff at all — include in review body
            body_only_findings.append(f)

    # Build summary
    severity_counts: dict[str, int] = {}
    for f in findings:
        sev = f["severity"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    summary_lines = ["### Multi-Provider Code Review\n"]
    providers_all = sorted({p for f in findings for p in f.get("providers", [])})
    summary_lines.append(f"Reviewed by: {', '.join(providers_all)}")
    summary_lines.append(f"Findings posted: {len(findings)} (vetted by human)\n")

    for f in findings:
        providers = ", ".join(f.get("providers", []))
        desc = f["description"][:100]
        summary_lines.append(
            f"- **[{f['severity']}]** `{f['file']}:{f['line']}` — {desc}... ({providers})"
        )

    # Add body-only findings (files not in diff) to the review body
    if body_only_findings:
        summary_lines.append("\n#### Findings on unchanged files\n")
        for f in body_only_findings:
            summary_lines.append(f"**[{f['severity']}]** `{f['file']}:{f['line']}`\n")
            summary_lines.append(build_comment_body(f))
            summary_lines.append("")

    review_body = "\n".join(summary_lines)

    # Build the review payload
    payload: dict = {
        "commit_id": commit_sha,
        "body": review_body,
        "event": "COMMENT",
    }
    if inline_comments:
        payload["comments"] = inline_comments

    # Post via gh api
    payload_json = json.dumps(payload)
    result = subprocess.run(
        [
            "gh", "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--method", "POST",
            "--input", "-",
        ],
        input=payload_json,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        print(f"Failed to post review: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    response = json.loads(result.stdout)
    return response.get("html_url", "review posted")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post approved findings as a GitHub PR review")
    parser.add_argument("findings_file", help="JSON file with approved findings")
    parser.add_argument("--repo", required=True, help="Owner/repo")
    parser.add_argument("--pr", required=True, help="PR number")
    parser.add_argument("--commit-sha", required=True, help="Head commit SHA")
    args = parser.parse_args()

    path = Path(args.findings_file)
    if not path.exists():
        print(f"File not found: {args.findings_file}", file=sys.stderr)
        sys.exit(1)

    findings = json.loads(path.read_text())
    if not findings:
        print("No findings to post.")
        sys.exit(0)

    url = post_review(args.repo, args.pr, args.commit_sha, findings)
    print(json.dumps({"status": "posted", "url": url, "count": len(findings)}))


if __name__ == "__main__":
    main()
