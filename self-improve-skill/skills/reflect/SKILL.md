---
name: reflect
description: Analyze the current session transcript and improve project documentation based on what was learned. Triggered automatically via SessionEnd hook, or manually with /reflect. Use when the user says "reflect", "what did we learn", "improve docs from this session", or "capture learnings". Also runs automatically in the background after every substantial session.
---

# Reflect

Analyze a session transcript to extract learnings and apply them as improvements to the project's documentation — CLAUDE.md, README.md, and auto-memory files.

## What This Skill Does

After a session ends (or when invoked manually), reflect reads through the transcript and looks for:

- **Corrections** — The user said "no", "not like that", corrected output, or Claude had to retry something that failed
- **Discoveries** — New patterns, conventions, or constraints that emerged during the work
- **Pain points** — Workarounds, confusing APIs, tricky configurations that cost time
- **Decisions** — Architectural or design choices made during the session that future sessions should know about

Then it determines which project files would benefit from capturing these learnings and edits them directly.

## Modes

### Interactive Mode (default)

When a user runs `/self-improve-skill:reflect` in conversation:

1. Summarize what happened in the session (2-3 sentences)
2. List the learnings found, grouped by confidence (HIGH/MEDIUM/LOW)
3. For each learning, show which file would be edited and the proposed change
4. Ask the user to approve, modify, or skip each change
5. Apply approved changes

### Non-Interactive Mode (`--non-interactive` flag)

When invoked by the SessionEnd hook via `claude -p "/self-improve-skill:reflect --non-interactive" < transcript.jsonl`:

- Parse the JSONL transcript from stdin
- Analyze the session
- Apply only HIGH confidence changes directly
- Write MEDIUM/LOW confidence observations to the memory directory for later review
- Do NOT run any git commands
- Output a summary of changes made to stdout

Detect mode by checking if `--non-interactive` is present in the arguments.

## Target Files

Only edit files within the current repository. Never edit global files like `~/.claude/CLAUDE.md`.

### CLAUDE.md

Add or update instructions that would prevent repeating mistakes or capture conventions discovered during the session. Examples:
- "Always run `npm run typecheck` before committing — the CI check is strict"
- "The `legacy/` directory uses CommonJS, not ESM"
- "Database migrations must be backwards-compatible (blue-green deploys)"

Place new entries in the most relevant existing section. If no section fits, append to the end. Keep entries concise — one line per instruction when possible.

### README.md

Update if the session revealed outdated setup instructions, missing prerequisites, or incorrect documentation. Only edit sections that are clearly wrong or missing critical information.

### Auto-memory (`~/.claude/projects/.../memory/`)

Write observations that are useful but not yet confirmed across multiple sessions. Memory is a good staging ground — things can graduate to CLAUDE.md once they prove stable. Organize by topic file (e.g., `debugging.md`, `patterns.md`), not chronologically.

## How to Analyze

Read the transcript looking for these signals:

**HIGH confidence (apply in non-interactive mode):**
- User explicitly corrected Claude and the correction reveals a project convention
- A command failed and the fix reveals an environment requirement
- User stated a preference or rule directly ("always use...", "never do...")

**MEDIUM confidence (memory only in non-interactive mode):**
- Patterns observed but not explicitly stated by the user
- Workarounds that might be temporary
- Tool/library preferences shown implicitly

**LOW confidence (memory only in non-interactive mode):**
- Single-occurrence observations
- Things that might be session-specific rather than project-wide

## Important Constraints

- Never duplicate information already in CLAUDE.md or memory files — check first
- Never remove existing content unless it's clearly contradicted by session evidence
- Keep edits minimal and surgical — add what's needed, nothing more
- If a CLAUDE.md file doesn't exist yet, do not create one — only edit existing files
- If a memory directory doesn't exist, you may create it (this is standard Claude Code behavior)
- Prefer updating an existing memory file over creating a new one
- Never edit skill files, hook files, or plugin configurations
- Never run git add, git commit, or git push

## Example

After a session where the user struggled with TypeScript strict mode:

**CLAUDE.md addition:**
```
# TypeScript
- This project uses `strict: true` in tsconfig — always handle null/undefined explicitly
- Prefer `satisfies` over `as` for type assertions
```

**Memory file (`memory/typescript.md`):**
```
## Strict mode patterns observed
- User prefers `satisfies` operator over type assertions
- Nullable fields in API responses need explicit checks, not non-null assertions
```
