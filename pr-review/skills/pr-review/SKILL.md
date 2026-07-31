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
5. **Post** — submit approved findings as inline file-level comments (deterministic script)
6. **Verdict** — draft a PR-level review (approve/request changes/comment), let user edit, then post

Scripts in `scripts/` handle deterministic work (prompt building, GitHub posting). The LLM handles semantic work (reading raw outputs, clustering duplicates, synthesizing consensus, interactive discussion).

## Step 0: Locate the plugin

`CLAUDE_PLUGIN_ROOT` is set by Claude Code only — **Codex sets no plugin-root variable
at all**, so it would expand to an empty string. Resolve the root once, then reuse it:

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -f "$PLUGIN_ROOT/scripts/build_prompts.py" ] || PLUGIN_ROOT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents -path "*pr-review*" \
       -name build_prompts.py 2>/dev/null | head -1 | xargs -r dirname | xargs -r dirname
)
echo "$PLUGIN_ROOT"
```

## Step 1: Build Prompts

Run the prompt builder script to fetch PR metadata and create prompt files:

```bash
uv run "$PLUGIN_ROOT/scripts/build_prompts.py" <PR_NUMBER> --out-dir /tmp
```

This outputs JSON with paths to per-provider prompts:
```json
{
  "pr_number": 365,
  "pr_title": "MEWS Integration",
  "pr_url": "https://github.com/owner/repo/pull/365",
  "repo": "owner/repo",
  "commit_sha": "abc123...",
  "review_prompt_claude": "/tmp/pr-review-prompt-claude-365.md",
  "review_prompt_gemini": "/tmp/pr-review-prompt-gemini-365.md",
  "review_prompt_codex": "/tmp/pr-review-prompt-codex-365.md",
  "security_prompt_claude": "/tmp/pr-security-prompt-claude-365.md",
  "security_prompt_gemini": "/tmp/pr-security-prompt-gemini-365.md",
  "security_prompt_codex": "/tmp/pr-security-prompt-codex-365.md"
}
```

Each provider gets tailored review AND security prompts:
- **Claude**: Multi-agent code review (5 parallel Sonnet agents + Haiku scoring) + `/security-review` built-in command with structured output
- **Gemini**: Principal Engineer persona with systematic analysis + Senior Security Engineer persona
- **Codex**: Focused single-pass reviews optimized for Codex's sandbox environment

Save all these values — you'll need them throughout.

## Step 2: Run All Reviewers in Parallel

### Select review profile

Read the profile definitions from `$PLUGIN_ROOT/skills/pr-review/references/profiles.md`
(resolve `$PLUGIN_ROOT` as in Step 0).

Determine which profile to use:
- If the user specified a profile (e.g., "review PR #365 with quality profile"), use that.
- Otherwise, default to **balanced**.

Extract the per-provider CLI flags for the selected profile. For example, with "balanced":
- Claude flags: `--model sonnet --effort high`
- Gemini flags: `-m gemini-2.5-flash`
- Codex flags: `-m o4-mini`

### Check available CLIs

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
 Profile: <profile>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Run all available reviewers as **parallel background Bash commands** using `run_in_background: true`. Each reviewer gets the PR URL and fetches context via its own tools — prompts are small (~1KB).

### Claude Code — Code Review
```bash
claude -p "$(cat <review_prompt_claude>)" <CLAUDE_FLAGS> > /tmp/pr-review-claude-code-<PR>.md 2>/dev/null
```

### Claude Code — Security Review
```bash
claude -p "$(cat <security_prompt_claude>)" <CLAUDE_FLAGS> > /tmp/pr-review-claude-security-<PR>.md 2>/dev/null
```

Where `<CLAUDE_FLAGS>` comes from the active profile (e.g., `--model sonnet --effort high`).

### Gemini CLI — Code Review
Gemini takes longer than Claude/Codex because it uses tools internally (reads files, spawns sub-agents). Its output includes a thinking trace before the structured findings — the parse script handles this.

**Important:** Gemini requires `--yolo` for headless execution. Without it, Gemini prompts for tool approval and exits with code 1. Pipe the prompt via stdin for reliability with large prompts.
```bash
cat <review_prompt_gemini> | gemini -p - <GEMINI_FLAGS> --yolo > /tmp/pr-review-gemini-code-<PR>.md 2>/dev/null
```

### Gemini CLI — Security Review
```bash
cat <security_prompt_gemini> | gemini -p - <GEMINI_FLAGS> --yolo > /tmp/pr-review-gemini-security-<PR>.md 2>/dev/null
```

Where `<GEMINI_FLAGS>` comes from the active profile (e.g., `-m gemini-2.5-flash`).

### Codex CLI — Code Review
Codex runs in a sandbox that blocks network access by default. Use `--sandbox danger-full-access` so it can call `gh` to fetch the PR.
```bash
codex exec <CODEX_FLAGS> --sandbox danger-full-access --skip-git-repo-check \
  "$(cat <review_prompt_codex>)" > /tmp/pr-review-codex-code-<PR>.md 2>/dev/null
```

### Codex CLI — Security Review
```bash
codex exec <CODEX_FLAGS> --sandbox danger-full-access --skip-git-repo-check \
  "$(cat <security_prompt_codex>)" > /tmp/pr-review-codex-security-<PR>.md 2>/dev/null
```

Where `<CODEX_FLAGS>` comes from the active profile (e.g., `-m o4-mini`).

**Important:** For all background Bash commands, use `timeout: 600000` (10 minutes). Gemini and Codex can take significantly longer than Claude — especially Gemini which spawns sub-agents internally. Allow up to 30 minutes for the full review by setting `timeout: 1800000` on the Bash calls for Gemini and Codex.

As each background command completes, report progress:
```
 Profile: <profile>
 Reviewing with Claude (code)...        done
 Reviewing with Claude (security)...    done
 Reviewing with Gemini (code)...        done
 Reviewing with Gemini (security)...    done
 Reviewing with Codex CLI (code)...     done
 Reviewing with Codex CLI (security)... done
```

### Handling failures

- If a reviewer fails or times out, log it and continue with the others
- Gemini may exit non-zero even with usable output — always check the output file before discarding
- Gemini requires `--yolo` for headless execution — without it, it prompts for tool approval and exits with code 1
- Gemini quota errors (HTTP 429 / `TerminalQuotaError`) mean the user's free tier is exhausted — skip Gemini and note in the summary
- Codex can be slow on large PRs (fetching diff + analysis) — if output is empty after timeout, it likely failed silently or timed out mid-analysis

## Step 3: Synthesize Consensus

This follows the same pattern as GSD's cross-AI review: read all raw reviewer outputs, then synthesize a consensus.

Read all the reviewer output files from step 2 using the Read tool:
- `/tmp/pr-review-claude-code-<PR>.md`
- `/tmp/pr-review-claude-security-<PR>.md`
- `/tmp/pr-review-gemini-code-<PR>.md`
- `/tmp/pr-review-gemini-security-<PR>.md`
- `/tmp/pr-review-codex-code-<PR>.md`
- `/tmp/pr-review-codex-security-<PR>.md`

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

## Step 5: Post Approved Findings as Inline File Comments

**All findings MUST be posted as inline line-level comments on the specific files, NOT as a single body-level PR comment.** The post script handles mapping findings to diff lines automatically — if a finding's exact line isn't in the diff, it attaches to the nearest changed line with a note.

Write the approved findings to a JSON file (one entry per finding with file, line, severity, category, confidence, description, suggestion, providers), then run the post script:

```bash
uv run "$PLUGIN_ROOT/scripts/post_review.py" \
  /tmp/pr-review-approved-<PR>.json \
  --repo <repo> --pr <PR> --commit-sha <commit_sha>
```

For clustered findings (same root issue, multiple files), post a comment on the primary file/line and mention the related locations in the comment body.

### If posting fails with 422

A common cause is a stale pending review. Check for and delete it:
```bash
# Find pending reviews
gh api repos/<repo>/pulls/<PR>/reviews --jq '.[] | select(.state == "PENDING") | .id'

# Delete the pending review
gh api repos/<repo>/pulls/<PR>/reviews/<REVIEW_ID> --method DELETE
```

Then retry posting.

### If no findings approved

If the user rejected everything, confirm:
```
All findings were rejected. No comments will be posted to the PR.
```

## Step 6: Draft PR-Level Review Verdict

After posting inline comments, draft a **PR-level review comment** with a verdict. This is the top-level review summary that accompanies the inline comments.

1. **Draft the review body** — Summarize the review: how many findings, what the most critical issues are, and your overall assessment. Keep it concise (3-5 sentences). Present the full draft text to the user.

2. **Ask the user to edit the draft** via AskUserQuestion — This is a SEPARATE step BEFORE asking for the verdict. Present the draft and ask:
   - **Looks good** — Accept the draft as-is
   - **Edit** — User wants to modify the text (ask what to change, apply edits, show the updated draft, and repeat until they confirm)

   Do NOT combine this step with the verdict question. The user MUST have a chance to edit the text first.

3. **Ask for the verdict** via AskUserQuestion — Only AFTER the user has confirmed the final text:
   - **Request Changes** — Has blocking issues that must be fixed before merge
   - **Approve** — No blocking issues, safe to merge (possibly with minor suggestions)
   - **Comment** — Neutral, just sharing observations without a verdict

4. **Post the verdict** — Submit the PR-level review with the confirmed text and chosen event type (`APPROVE`, `REQUEST_CHANGES`, or `COMMENT`):
   ```bash
   gh api repos/<repo>/pulls/<PR>/reviews \
     --method POST \
     --input - <<< '{"commit_id": "<sha>", "body": "<review body>", "event": "<APPROVE|REQUEST_CHANGES|COMMENT>"}'
   ```

**Important:** This step posts ONLY the PR-level verdict. The inline file comments were already posted in Step 5. Do NOT include inline comments again here.

## Step 7: Cleanup and Summary

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

 Verdict: <APPROVE|REQUEST_CHANGES|COMMENT>
 Inline comments: <PR URL>
 Review verdict:  <PR URL>
```

## Error Handling

- If a reviewer CLI fails or times out (>5 min), log it and continue with the others
- Gemini may exit non-zero even with usable output — always check the output file
- If no CLIs are available, tell the user which ones to install
- If `gh` is not authenticated, tell the user to run `gh auth login`
- If the PR is already merged or closed, warn and ask whether to proceed anyway
