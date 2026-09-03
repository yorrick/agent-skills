# Task Status Plugin Implementation Plan

**Goal:** Ship one read-only `task-status` Agent Skill that produces the same
compact vertical status board in Claude Code and Codex.

**Design:** `docs/superpowers/specs/2026-09-02-task-status-plugin-design.md`

Before Task 1, create a `feat/task-status-plugin` branch from the current clean
`main` baseline and carry the reviewed design/plan changes onto it. Do not push
the branch yet.

## Task 1: Add failing contract coverage

Create `tests/test_task_status_skill.py` before the plugin files. Cover:

- required `SKILL.md` path and portable frontmatter;
- exact five lane markers and this exact first instruction: “Use no tools.
  Answer only from the current conversation. If a tool would be needed to know
  a status, put that verification under Next.”;
- absence of Mermaid, table pipes, ANSI escapes, box drawing U+2500 through
  U+257F, and block elements U+2580 through U+259F;
- absence of known HTML tags rather than angle-bracket placeholders generally;
- nested `policy.allow_implicit_invocation: false` and
  `interface.default_prompt` in `agents/openai.yaml`; and
- generated Claude/Codex manifests exposing `skills` only where appropriate.

Use only the Python standard library. Compare `agents/openai.yaml` to the exact
expected text instead of adding a YAML dependency. Keep the skill description
on one quoted line, so the repository's flat frontmatter parser and the test do
not need YAML block-scalar support.

The current `.venv/bin/pytest` shebang still points at the repository's former
`claude-code-plugins` path, and a normal sync does not reinstall it. Run
`uv sync --reinstall`, then `uv run pytest tests/test_task_status_skill.py` and
retain the expected RED failure caused by the missing plugin. A bad-interpreter
or environment failure is not an acceptable RED result.

## Task 2: Implement the shared skill

Create:

- `task-status/plugin.toml` at version `0.1.0` with MIT metadata;
- `task-status/skills/task-status/SKILL.md` containing the approved read-only,
  evidence, lane, compaction, and concision rules; and
- `task-status/skills/task-status/agents/openai.yaml` disabling implicit Codex
  invocation and providing a `$task-status` default prompt.

The skill must include the exact first instruction asserted by Task 1, treat
invocation arguments as conversation text without letting them override its
rules or evidence boundary, treat pasted/file/tool content as evidence rather
than instructions, reject embedded success claims without visible passing
evidence, handle no active task, omit empty optional lanes, define `Now` as the
prior activity, and disclose summarized-context use. Its description must be
80 to 1,024 characters and activate only for an explicit task-status request.

The policy file is exactly:

```yaml
interface:
  display_name: "Task Status"
  short_description: "Show a compact visual summary of the current task"
  default_prompt: "Use $task-status to summarize the current task."
policy:
  allow_implicit_invocation: false
```

Run `uv run scripts/sync_manifests.py`, then rerun the contract test to GREEN.
Commit all generated outputs: both per-plugin manifests plus
`.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json`. Assert
that the Codex manifest has `"skills": "./skills/"` and no `commands` or
`hooks`, while the Claude manifest has none of those component keys.

## Task 3: Document and gate the plugin

Update `README.md` with purpose, invocation forms, local development, and install
commands for both harnesses. Update `AGENTS.md` so its plugin layout includes
`agents/openai.yaml` and its validation checklist includes the contract tests.
`CLAUDE.md` is the same symlinked file and must not be edited separately.

Update `.github/workflows/ci.yml` to lint `scripts/ tests/`, revise the stale
lint-scope comment, run `uv run pytest tests/` after lint, and run the now-green
root `uv run pyright` gate. `uv run` provisions the dev dependency group in CI,
so CI needs no separate sync step.
Add `__pycache__/` to the repository `.gitignore` because the new Python contract
test creates that directory locally.

The root Pyright gate is currently red because `[tool.pyright].exclude` replaced
Pyright's default hidden-directory exclusion and admitted `.venv` (5,379
third-party errors), while `uv run pyright scripts/` is clean. Add `**/.venv` to
the explicit exclusion in `pyproject.toml` so the repository-required root
`uv run pyright` gate becomes meaningful and green, including nested
worktrees.

## Task 4: Validate behavior and repository health

Run:

```text
uv run scripts/sync_manifests.py --check
uv run scripts/validate_skills.py
uv run pytest tests/
uv run pytest dev-loop/tests/test_engine.py
uv run ruff check scripts/ tests/
uv run ruff format --check .
uv run pyright
git diff --check
```

Run a Claude Code local-plugin smoke with `--plugin-dir ./task-status --print`, a
synthetic single-turn conversation containing completed, active, next,
deferred, blocked, and untrusted-output examples, and an explicit request for
task status. Use natural-language activation plus
`--output-format stream-json --verbose`; require the five lane markers,
vertical concise output, exactly one Skill tool-use event naming
`task-status:task-status`, and no other tool-use event. This isolates the
skill-loading event from prohibited execution tools.

Run the Codex smoke from a `mktemp -d` Git repository whose
`.agents/skills/task-status` is a temporary copy of the new skill. Invoke
`codex exec --json --sandbox read-only --disable plugins -C
<temporary-repository>` with the same synthetic context plus `$task-status`;
require the same markers and zero command or tool events. Disabling marketplace
plugins prevents the otherwise automatic marketplace-upgrade thread while
leaving repository-scoped Agent Skill discovery available. Remove only the
resolved `mktemp` directory afterward. This tests actual Codex skill discovery
without modifying the user's installed plugin configuration or cache. A copy is
required because the isolated repository smoke did not discover a skill-folder
symlink whose target was outside that repository, while the copied fixture was
discovered and obeyed the no-tools rule.

## Task 5: Independent review and release

Protected `main` requires one approving PR review plus resolved conversations
and is an auto-update release channel. Run a complete read-only Claude Code
Fable implementation review. Verify every finding against source, fix accepted
findings with targeted tests, and repeat review until no HIGH or MEDIUM concern
remains.

Commit the reviewed change locally. Stop and request explicit user confirmation
before pushing the branch or opening/merging the PR. After approval, let the new
CI pytest step run and require the protected review before merge. Record and
report every exact Fable command and outcome.
