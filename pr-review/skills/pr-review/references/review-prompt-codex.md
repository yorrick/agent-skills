# Code Review Prompt (Codex)

Review pull request #{{PR_NUMBER}} in {{REPO}}.

Use `gh pr diff {{PR_NUMBER}}` to fetch the diff and `gh pr view {{PR_NUMBER}}` for PR metadata.
Read the project's CLAUDE.md file at the repo root (if it exists) for project guidelines.
You may also read full source files for additional context beyond the diff.

## Focus areas

- **Bugs**: Logic errors, off-by-one, null/undefined handling, race conditions, incorrect return values
- **Security**: Injection vulnerabilities, auth bypasses, data exposure, insecure defaults
- **Logic**: Incorrect algorithm, missing edge cases, wrong assumptions about data
- **Performance**: Obvious N+1 queries, unnecessary allocations in hot paths
- **Guideline violations**: Only if CLAUDE.md explicitly calls something out

## What to ignore

- Pre-existing issues on lines that weren't modified
- Style nitpicks not explicitly required by CLAUDE.md
- Issues a linter, type checker, or compiler would catch
- Missing tests (unless CLAUDE.md explicitly requires them)
- General code quality opinions without concrete impact
- Changes in functionality that are clearly intentional

## Output format

For each issue, output in this exact format (one issue per block, separated by `---`):

```
FILE: <relative file path>
LINE: <line number in the new version of the file>
SEVERITY: <CRITICAL|HIGH|MEDIUM|LOW>
CATEGORY: <bug|security|style|performance|logic|guideline-violation>
CONFIDENCE: <0-100>
DESCRIPTION: <what the issue is, be specific>
SUGGESTION: <how to fix it, be concrete>
---
```

Confidence: 90-100 = verified real issue, 75-89 = very likely, 50-74 = possible, below 50 = don't report.

Only report findings with confidence >= 50. If no issues found, output: `NO_ISSUES`
