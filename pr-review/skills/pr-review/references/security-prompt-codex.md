# Security Review Prompt (Codex)

Perform a security review of pull request #{{PR_NUMBER}} in {{REPO}}.

Use `gh pr diff {{PR_NUMBER}}` to fetch the diff and `gh pr view {{PR_NUMBER}}` for metadata.
Read full source files referenced in the diff for additional context.

## Focus areas (OWASP-aligned)

1. **Injection** — SQL, NoSQL, OS command injection via unsanitized input
2. **Broken Authentication** — Missing auth checks, weak session management
3. **Sensitive Data Exposure** — Secrets in code, PII logging, insecure transmission
4. **Broken Access Control** — Missing authorization, IDOR, privilege escalation
5. **SSRF** — Server-side requests to user-controlled URLs
6. **Path Traversal** — File operations with user-controlled paths
7. **Secrets in Code** — Hardcoded API keys, tokens, passwords
8. **Dependency Risks** — Unmaintained or vulnerable new dependencies

## What to ignore

- Pre-existing security issues on unchanged lines
- Theoretical issues without a concrete attack path
- General security best practices that don't apply to the specific changes

## Output format

For each issue, output in this exact format (one issue per block, separated by `---`):

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

Confidence: 90-100 = verified exploitable, 75-89 = very likely, 50-74 = potential, below 50 = don't report.

Only report findings with confidence >= 50. If no issues found, output: `NO_ISSUES`
