/security-review

Review the changes in PR #{{PR_NUMBER}} ({{PR_URL}}).

Use `gh pr diff {{PR_NUMBER}}` to fetch the diff and `gh pr view {{PR_NUMBER}}` for metadata.
Read the project's CLAUDE.md file at the repo root (if it exists) for project guidelines.
Read full source files referenced in the diff for additional context — security issues often depend on how functions are called, not just how they're defined.

IMPORTANT: Do NOT post a comment on the PR. Do NOT use gh pr comment. Instead, output your findings in this exact structured format (one issue per block, separated by `---`):

```
FILE: <relative file path>
LINE: <line number>
SEVERITY: <CRITICAL|HIGH|MEDIUM|LOW>
CATEGORY: security
CONFIDENCE: <0-100>
DESCRIPTION: <what the vulnerability is>
SUGGESTION: <how to remediate>
---
```

Confidence scoring:
- 90-100: Verified vulnerability with clear exploit path
- 75-89: Very likely exploitable, minor context uncertainty
- 50-74: Potential issue, may depend on deployment context
- Below 50: Don't report

Only report findings with confidence >= 50. If no issues found, output: `NO_ISSUES`
