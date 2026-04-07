---
name: pr-review
description: "Multi-provider interactive PR code review. Runs Claude Code, Gemini CLI, and Codex CLI in parallel to review a pull request, merges and deduplicates findings by severity, then walks through each issue interactively so you can vet, discuss, and selectively post comments. Use this skill when the user wants to review a PR, asks for code review on a pull request, says 'review PR #123', 'code review this PR', 'check this PR for issues', or mentions reviewing changes before merging. Also triggers on 'multi-provider review', 'cross-AI review', or 'security review PR'."
---

# Multi-Provider Interactive PR Review

Run three AI code reviewers in parallel on a PR, synthesize their findings, then interactively vet each issue before posting anything to GitHub.

The key principle: **nothing gets posted to the PR until you've reviewed and approved it.** You stay in control of every comment.

## How It Works

1. **Gather** — build prompts from PR metadata (deterministic script)
2. **Review** — run Claude, Gemini, and Codex in parallel (none of them touch the PR)
3. **Synthesize** — read all raw reviewer outputs, cluster by root issue, build consensus (LLM)
4. **Vet** — walk through each finding interactively, discuss, approve or reject (LLM + user)
5. **Post** — submit only approved findings as a GitHub review (deterministic script)

Scripts in `scripts/` handle deterministic work (prompt building, GitHub posting). The LLM handles semantic work (reading raw outputs, clustering duplicates, synthesizing consensus, interactive discussion).

## Step 1: Build Prompts

Run the prompt builder script to fetch PR metadata and create prompt files:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/build_prompts.py" <PR_NUMBER> --out-dir /tmp
```

This outputs JSON with paths to the generated prompts:
```json
{
  "pr_number": 365,
  "pr_title": "MEWS Integration",
  "pr_url": "https://github.com/owner/repo/pull/365",
  "repo": "owner/repo",
  "commit_sha": "abc123...",
  "review_prompt": "/tmp/pr-review-prompt-365.md",
  "security_prompt": "/tmp/pr-review-security-prompt-365.md"
}
```

Save all these values — you'll need them throughout.

## Step 2: Run All Reviewers in Parallel

Check which CLIs are available:

```bash
command -v claude >/dev/null 2>&1  # Claude Code
command -v gemini >/dev/null 2>&1  # Gemini CLI
command -v codex >/dev/null 2>&1   # Codex CLI
```

At least one CLI must be available. If none are found, tell the user which to install and stop.

Display the header before launching:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 PR REVIEW — #<number>: <title>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Run all available reviewers as **parallel background Bash commands** using `run_in_background: true`. Each reviewer gets the PR URL and fetches context via its own tools — prompts are small (~1KB).

### Claude Code — Code Review
```bash
claude -p "$(cat <review_prompt>)" > /tmp/pr-review-claude-code-<PR>.md 2>/dev/null
```

### Claude Code — Security Review
```bash
claude -p "$(cat <security_prompt>)" > /tmp/pr-review-claude-security-<PR>.md 2>/dev/null
```

### Gemini CLI — Code Review
Gemini takes longer than Claude/Codex because it uses tools internally (reads files, spawns sub-agents). Its output includes a thinking trace before the structured findings — the parse script handles this.
```bash
gemini -p "$(cat <review_prompt>)" > /tmp/pr-review-gemini-code-<PR>.md 2>/dev/null
```

### Gemini CLI — Security Review
```bash
gemini -p "$(cat <security_prompt>)" > /tmp/pr-review-gemini-security-<PR>.md 2>/dev/null
```

### Codex CLI — Code Review
Codex runs in a sandbox that blocks network access by default. Use `--sandbox danger-full-access` so it can call `gh` to fetch the PR.
```bash
codex exec --sandbox danger-full-access --skip-git-repo-check \
  "$(cat <review_prompt>)" > /tmp/pr-review-codex-<PR>.md 2>/dev/null
```

As each background command completes, report progress:
```
 Reviewing with Claude (code)...     done
 Reviewing with Claude (security)... done
 Reviewing with Gemini (code)...     done
 Reviewing with Gemini (security)... done
 Reviewing with Codex CLI...         done
```

### Handling failures

- If a reviewer fails or times out (>5 minutes), log it and continue with the others
- Gemini may exit non-zero even with usable output — always check the output file before discarding

## Step 3: Synthesize Consensus

This follows the same pattern as GSD's cross-AI review: read all raw reviewer outputs, then synthesize a consensus.

Read all the reviewer output files from step 2 using the Read tool:
- `/tmp/pr-review-claude-code-<PR>.md`
- `/tmp/pr-review-claude-security-<PR>.md`
- `/tmp/pr-review-gemini-code-<PR>.md`
- `/tmp/pr-review-gemini-security-<PR>.md`
- `/tmp/pr-review-codex-<PR>.md`

Skip any files that don't exist (reviewer failed or was unavailable).

### Build the consensus

Like GSD's "Agreed Concerns / Divergent Views" pattern, group findings into clusters:

**Agreed Concerns** — Issues raised by 2+ providers (highest priority). Multiple reviewers independently finding the same issue is strong signal. When clustering:
- Group findings that describe the same root issue, even across different files/lines. For example, "SSRF via platform_url" flagged in models.py by one provider and in endpoint.py by another is the same vulnerability.
- Pick the most comprehensive description as the primary
- List all affected files/lines
- Combine provider lists — "Found by: claude, gemini, codex" carries more weight than a single provider

**Unique Concerns** — Issues found by only one provider but with high confidence (>= 75). These are worth reviewing because different models have different blind spots.

**Low-confidence** — Findings with confidence < 75 from a single provider. Present last and offer batch reject.

Sort clusters by:
1. Multi-provider agreed concerns first (CRITICAL > HIGH > MEDIUM > LOW)
2. Single-provider unique concerns second (CRITICAL > HIGH > MEDIUM > LOW)  
3. Low-confidence findings last

## Step 4: Interactive Vetting Loop

Walk through each finding/cluster one at a time, starting with the most critical agreed concerns.

For each finding, present it and ask with AskUserQuestion:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Finding <N>/<total>  [<SEVERITY>]  <category>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 Files: <file>:<line>, <file2>:<line2>
 Found by: <providers> (confidence: <score>)

 <description>

 Suggested fix: <suggestion>
```

AskUserQuestion options:

- **Approve** — include this finding in the PR review comment
- **Reject** — skip this finding, don't post it
- **Discuss** — ask questions about this finding before deciding
- **Edit** — approve but modify the comment text first
- **Investigate** — run the code locally to verify the behavior before deciding

If the user selects **Discuss**, answer their questions about the finding using the PR diff context and your own understanding of the code. After the discussion, re-present the approve/reject/edit/investigate choice.

If the user selects **Edit**, ask them what they'd like to change about the comment text via AskUserQuestion, apply the edit, confirm the final text, then mark it as approved.

If the user selects **Investigate**, do a dynamic analysis of the finding:

1. **Figure out how to run the project locally.** Read the project's CLAUDE.md, README, Makefile, package.json, pyproject.toml, docker-compose.yml, or similar files to determine the dev server command. Common patterns:
   - Python: `uvicorn`, `flask run`, `python manage.py runserver`
   - Node: `npm run dev`, `next dev`, `tsx watch`
   - Check if a dev server is already running (e.g., check if a port is in use)

2. **Start the server** in a tmux session if not already running. Use a named session like `pr-review-server` so it can be cleaned up later.

3. **Craft a request** that exercises the code path referenced by the finding. Use the finding's file/line context to understand what endpoint or function to call and what input would trigger the issue.

4. **Run the test** and show the actual behavior to the user — the request, the response, and any relevant log output.

5. **Present the evidence** and re-ask the approve/reject/edit choice with the runtime behavior as additional context.

Not every finding can be investigated this way (e.g., race conditions, deployment-specific issues). If investigation isn't practical for a finding, explain why and fall back to the discuss flow.

Track all approved findings with their final comment text.

### Batch shortcuts

After presenting the first 3 findings individually, also offer:
- **Approve remaining LOW** — auto-approve all remaining LOW severity findings
- **Reject remaining LOW** — auto-reject all remaining LOW severity findings

These only apply to LOW severity — CRITICAL, HIGH, and MEDIUM always require individual review.

## Step 5: Post Approved Comments to PR

Write the approved findings to a JSON file (one entry per finding with file, line, severity, category, confidence, description, suggestion, providers), then run the post script:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/scripts/post_review.py" \
  /tmp/pr-review-approved-<PR>.json \
  --repo <repo> --pr <PR> --commit-sha <commit_sha>
```

This creates a single atomic GitHub review with individual line-level comments. The review body includes a summary of all approved findings.

For clustered findings (same root issue, multiple files), post a comment on the primary file/line and mention the related locations in the comment body.

### If no findings approved

If the user rejected everything, confirm:
```
All findings were rejected. No comments will be posted to the PR.
```

## Step 6: Cleanup and Summary

Delete temp files:
```bash
rm -f /tmp/pr-review-*-<PR>.*
```

Show final summary:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 PR REVIEW COMPLETE — #<number>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

 Total findings: <N> (<N> clusters)
 Approved: <N>  Rejected: <N>  Edited: <N>

 Agreed concerns (2+ providers): <N>
 Unique concerns (1 provider):   <N>

 By provider:
   Claude:  <N> findings (<N> approved)
   Gemini:  <N> findings (<N> approved)
   Codex:   <N> findings (<N> approved)

 By severity:
   CRITICAL: <N>  HIGH: <N>  MEDIUM: <N>  LOW: <N>

 Review posted: <PR URL>
```

## Error Handling

- If a reviewer CLI fails or times out (>5 min), log it and continue with the others
- Gemini may exit non-zero even with usable output — always check the output file
- If no CLIs are available, tell the user which ones to install
- If `gh` is not authenticated, tell the user to run `gh auth login`
- If the PR is already merged or closed, warn and ask whether to proceed anyway
