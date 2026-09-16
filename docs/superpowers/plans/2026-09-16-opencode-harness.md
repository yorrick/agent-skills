# opencode Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make this repository installable in opencode as a single git plugin spec that registers every plugin's skills and commands.

**Architecture:** One hand-written opencode plugin entry (`.opencode/plugins/agent-skills.js`) discovers plugin directories at load time using the repo's existing `plugin.toml` invariant, then mutates opencode's resolved config to add skills paths and commands. One new generated file (`package.json`, produced by `scripts/sync_manifests.py`) gives opencode the package entry point with a version that cannot drift. A pytest contract test exercises the entry via a small node fixture, deriving expectations from the filesystem.

**Tech Stack:** Node builtins only (the entry has zero dependencies), Python 3.11+ with uv, pytest, ruff, pyright, existing manifest generator.

**Spec:** `docs/superpowers/specs/2026-09-16-opencode-harness-design.md`

## Global Constraints

- The entry must use only node builtins (`node:fs`, `node:path`, `node:url`); no dependencies, so CI needs no `npm install`.
- The entry must export only functions (opencode's legacy plugin loader throws on non-function exports).
- `package.json` is generated; never hand-edit it. Regenerate with `uv run scripts/sync_manifests.py`.
- The plugin detection invariant is: a top-level directory is a plugin if and only if it contains `plugin.toml`, and the directory name equals the plugin name.
- Do not modify `SKILL.md` frontmatter or any generated `plugin.json` / `marketplace.json` by hand.
- All quality gates must pass before claiming done: `uv run scripts/sync_manifests.py --check`, `uv run scripts/validate_skills.py`, `uv run pytest tests/`, `uv run ruff format --check .`, `uv run ruff check scripts/ tests/`, `uv run pyright`.
- Commit messages follow the repo style: conventional prefix plus a body explaining the reason.
- Node is available locally (v23) and on GitHub's ubuntu runners.

---

### Task 1: Generate the root `package.json`

**Files:**
- Modify: `scripts/sync_manifests.py`
- Create (generated): `package.json`

**Interfaces:**
- Consumes: `load_plugins()` (existing, returns `list[dict]` of plugin metadata with `name`, `version`, `description`).
- Produces: `package_json(plugins: list[dict]) -> dict` and `version_tuple(version: str) -> tuple[int, int, int]`; a new key in the dict returned by `targets()`; `PACKAGE_NAME = "yorrick-agent-skills"`.

- [ ] **Step 1: Confirm `package.json` is not gitignored**

Run: `git check-ignore -v package.json`
Expected: no output, exit status 1. If it is ignored, the ignore rule must be fixed first.

- [ ] **Step 2: Add the package name constant and update the marketplace description**

In `scripts/sync_manifests.py`, after `REPOSITORY_URL`, add:

```python
PACKAGE_NAME = "yorrick-agent-skills"
```

and change:

```python
MARKETPLACE_DESCRIPTION = "Agent skills and plugins by Yorrick Jansen (Claude Code and Codex)"
```

to:

```python
MARKETPLACE_DESCRIPTION = "Agent skills and plugins by Yorrick Jansen (Claude Code, Codex, and opencode)"
```

- [ ] **Step 3: Add the version helper and the package manifest builder**

In `scripts/sync_manifests.py`, after `marketplace_manifest`, add:

```python
def version_tuple(version: str) -> tuple[int, int, int]:
    """Parse a plain x.y.z version, raising rather than guessing."""
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"version '{version}' is not plain x.y.z")
    return int(parts[0]), int(parts[1]), int(parts[2])


def package_json(plugins: list[dict]) -> dict:
    """The opencode package manifest.

    opencode installs this repository as a git package and resolves `main` to
    the plugin entry, which registers every plugin's skills and commands.
    The version exists only to satisfy the package format -- opencode keys its
    git cache on the spec, not on this field -- so it is derived from the
    newest plugin version instead of being hand-edited, and cannot drift.
    """
    newest = max(version_tuple(p["version"]) for p in plugins)
    return {
        "name": PACKAGE_NAME,
        "version": ".".join(str(n) for n in newest),
        "description": MARKETPLACE_DESCRIPTION,
        "license": "MIT",
        "type": "module",
        "main": ".opencode/plugins/agent-skills.js",
    }
```

- [ ] **Step 4: Register the new target**

In `targets()`, after the two marketplace entries, add:

```python
    out[REPO / "package.json"] = package_json(plugins)
```

- [ ] **Step 5: Update the module docstring**

In the module docstring, change "this script derives all four generated files from it:" to "this script derives all five generated files from it:" and add this line after the two marketplace lines:

```
    package.json                        repo root, opencode
```

- [ ] **Step 6: Generate and inspect**

Run: `uv run scripts/sync_manifests.py`
Expected: `Wrote 3 manifest(s):` listing two `marketplace.json` files and `package.json`.

Run: `cat package.json`
Expected: version `2.0.0` (the max of all plugin versions), `"main": ".opencode/plugins/agent-skills.js"`, description mentioning opencode.

- [ ] **Step 7: Verify the check mode passes**

Run: `uv run scripts/sync_manifests.py --check`
Expected: `All 5 manifests current.`

- [ ] **Step 8: Commit**

```bash
git add scripts/sync_manifests.py package.json .claude-plugin/marketplace.json .agents/plugins/marketplace.json
git commit -m "feat(opencode): generate a root package.json for the opencode plugin spec

opencode installs a repository as a git package and needs a package.json
whose main is the plugin entry. Generating it from plugin.toml keeps the
version derived (the max plugin version) so it cannot drift the way the
hand-maintained marketplace versions did."
```

---

### Task 2: The opencode entry and its contract test

**Files:**
- Create: `tests/opencode_entry_harness.mjs`
- Create: `tests/test_opencode_entry.py`
- Create: `.opencode/plugins/agent-skills.js`

**Interfaces:**
- Consumes: the `plugin.toml` invariant and each plugin's `skills/` and `commands/` directories.
- Produces: `AgentSkillsPlugin` (the sole export, an async function returning `{ config }`); the harness output contract used by Task 6's E2E verification: `{ exports, config, configAfterSecondPass, seeded }`.

- [ ] **Step 1: Write the node fixture**

Create `tests/opencode_entry_harness.mjs`:

```js
// Fixture for tests/test_opencode_entry.py. Loads the opencode plugin entry,
// runs its config hook against a fresh config, again against the same config
// (idempotency), and once against a config that already defines a `workflow`
// command (user commands must win). Prints the result as JSON.
//
// Run with cwd set to the repository root.

import path from 'node:path';
import { pathToFileURL } from 'node:url';

const repoRoot = process.cwd();
const entry = path.join(repoRoot, '.opencode', 'plugins', 'agent-skills.js');
const module = await import(pathToFileURL(entry).href);
const hooks = await module.AgentSkillsPlugin({ client: {}, directory: repoRoot });

const fresh = {};
await hooks.config(fresh);
const afterFirstPass = JSON.parse(JSON.stringify(fresh));
await hooks.config(fresh);

const seeded = { command: { workflow: { template: 'USER TEMPLATE' } } };
await hooks.config(seeded);

process.stdout.write(
  JSON.stringify({
    exports: Object.fromEntries(
      Object.entries(module).map(([name, value]) => [name, typeof value]),
    ),
    config: fresh,
    configAfterSecondPass: afterFirstPass,
    seeded,
  }),
);
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_opencode_entry.py`:

```python
"""Contract tests for the opencode plugin entry.

The entry is the only thing that makes this repository installable in
opencode, and nothing else in CI would notice if it broke: the generated
manifests are harness-specific and the skill validator only reads
frontmatter. Expectations are derived from the filesystem, so adding or
removing a plugin cannot silently desynchronise the test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "opencode_entry_harness.mjs"
PLUGIN_DIRS = sorted(p.parent for p in REPO.glob("*/plugin.toml"))
SKILL_DIRS = sorted(d / "skills" for d in PLUGIN_DIRS if (d / "skills").is_dir())
COMMAND_FILES = sorted(f for d in PLUGIN_DIRS for f in (d / "commands").glob("*.md"))


@pytest.fixture(scope="module")
def entry_output() -> dict:
    node = shutil.which("node")
    assert node, "node is required to exercise the opencode plugin entry"
    result = subprocess.run(
        [node, str(HARNESS)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_entry_exports_only_functions(entry_output: dict) -> None:
    assert entry_output["exports"], "the entry must export the plugin function"
    assert all(kind == "function" for kind in entry_output["exports"].values())


def test_registers_exactly_the_plugin_skill_directories(entry_output: dict) -> None:
    registered = {Path(p).resolve() for p in entry_output["config"]["skills"]["paths"]}
    assert registered == {p.resolve() for p in SKILL_DIRS}


def test_every_registered_skill_path_contains_a_skill(entry_output: dict) -> None:
    for raw in entry_output["config"]["skills"]["paths"]:
        assert list(Path(raw).glob("*/SKILL.md")), f"no SKILL.md under {raw}"


def test_registers_exactly_the_plugin_commands(entry_output: dict) -> None:
    commands = entry_output["config"]["command"]
    assert sorted(commands) == sorted(f.stem for f in COMMAND_FILES)


def test_command_templates_are_usable(entry_output: dict) -> None:
    for name, command in entry_output["config"]["command"].items():
        template = command["template"]
        assert template.strip(), f"{name} has an empty template"
        assert "${CLAUDE_PLUGIN_ROOT}" not in template
        assert not template.lstrip().startswith("---"), f"{name} kept its frontmatter"


def test_plugin_root_substitutes_to_the_owning_plugin(entry_output: dict) -> None:
    template = entry_output["config"]["command"]["review-loop"]["template"]
    expected = f"{(REPO / 'dev-loop').as_posix()}/scripts/dev-loop.py"
    assert expected in template.replace("\\", "/")


def test_config_hook_is_idempotent(entry_output: dict) -> None:
    assert entry_output["config"] == entry_output["configAfterSecondPass"]


def test_user_commands_are_left_alone(entry_output: dict) -> None:
    assert entry_output["seeded"]["command"]["workflow"] == {"template": "USER TEMPLATE"}
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_opencode_entry.py -v`
Expected: FAIL, the node process exits non-zero because `.opencode/plugins/agent-skills.js` does not exist yet (`CalledProcessError`).

- [ ] **Step 4: Write the entry**

Create `.opencode/plugins/agent-skills.js`:

```js
/**
 * opencode plugin for the yorrick/agent-skills repository.
 *
 * opencode installs this repository as a git package and imports this file.
 * The config hook registers every plugin directory's skills and commands so
 * the repository works in opencode the same way it does in Claude Code and
 * Codex: no symlinks, no per-skill configuration.
 *
 * A top-level directory is a plugin if and only if it contains plugin.toml,
 * the same invariant scripts/sync_manifests.py enforces. That excludes docs/,
 * scripts/, tests/, and any local-only directory automatically.
 *
 * The same file also works as a project plugin when opencode is run from a
 * checkout of this repository, because the package root is derived from this
 * file's own location.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(dirname, '..', '..');
const PLUGIN_MARKER = 'plugin.toml';
const PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}';

function pluginDirectories() {
  return fs
    .readdirSync(repoRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(repoRoot, entry.name))
    .filter((dir) => fs.existsSync(path.join(dir, PLUGIN_MARKER)));
}

function parseCommandFile(source) {
  const match = source.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/);
  if (!match) return { body: source, description: undefined };
  let description;
  for (const line of match[1].split(/\r?\n/)) {
    const colon = line.indexOf(':');
    if (colon <= 0 || line.slice(0, colon).trim() !== 'description') continue;
    description = line
      .slice(colon + 1)
      .trim()
      .replace(/^["']|["']$/g, '');
  }
  return { body: source.slice(match[0].length), description };
}

export const AgentSkillsPlugin = async () => {
  let directories;

  const allDirectories = () => (directories ??= pluginDirectories());

  const registerSkills = (config) => {
    config.skills = config.skills ?? {};
    const paths = (config.skills.paths = config.skills.paths ?? []);
    for (const dir of allDirectories()) {
      const skills = path.join(dir, 'skills');
      if (fs.existsSync(skills) && !paths.includes(skills)) paths.push(skills);
    }
  };

  const registerCommands = (config) => {
    config.command = config.command ?? {};
    for (const dir of allDirectories()) {
      const commands = path.join(dir, 'commands');
      if (!fs.existsSync(commands)) continue;
      for (const entry of fs.readdirSync(commands, { withFileTypes: true })) {
        if (!entry.isFile() || !entry.name.endsWith('.md')) continue;
        const name = entry.name.slice(0, -'.md'.length);
        if (Object.hasOwn(config.command, name)) continue;
        const source = fs.readFileSync(path.join(commands, entry.name), 'utf8');
        const { body, description } = parseCommandFile(source);
        const command = { template: body.split(PLUGIN_ROOT).join(dir) };
        if (description !== undefined) command.description = description;
        config.command[name] = command;
      }
    }
  };

  return {
    config: async (config) => {
      registerSkills(config);
      registerCommands(config);
    },
  };
};
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_opencode_entry.py -v`
Expected: 8 passed.

- [ ] **Step 6: Run the Python quality gates**

Run: `uv run ruff format tests/test_opencode_entry.py && uv run ruff check scripts/ tests/ && uv run pyright`
Expected: formatting applied if needed, no lint errors, no type errors.

- [ ] **Step 7: Commit**

```bash
git add .opencode/plugins/agent-skills.js tests/test_opencode_entry.py tests/opencode_entry_harness.mjs
git commit -m "feat(opencode): register all plugin skills and commands from one entry

The entry discovers plugin directories by the plugin.toml invariant the
manifest generator already enforces, so a new plugin needs no entry
changes. Command templates get the owning plugin's absolute path
substituted for CLAUDE_PLUGIN_ROOT, which opencode does not set, and an
existing command of the same name is never overwritten.

The contract test runs the entry under node and derives its expectations
from the filesystem, so the entry cannot silently rot in CI."
```

---

### Task 3: Make the skill script fallbacks find opencode's package cache

**Files:**
- Modify: `supabase-security/skills/supabase-security/SKILL.md:259-268`
- Modify: `pr-review/skills/pr-review/SKILL.md:23-35`
- Modify: any other skill that shells out to `find ~/.claude/plugins`

**Interfaces:**
- Consumes: nothing new.
- Produces: unchanged skill behaviour in Claude Code and Codex; working resolution in opencode, where packages live under `~/.cache/opencode/packages`.

- [ ] **Step 1: Find every affected skill**

Run: `rg -l 'find ~/.claude/plugins' --glob '!docs/**'`
Expected: `supabase-security/skills/supabase-security/SKILL.md` and `pr-review/skills/pr-review/SKILL.md` (extend any additional file the same way).

- [ ] **Step 2: Extend the supabase-security fallback**

In `supabase-security/skills/supabase-security/SKILL.md`, change the prose sentence

```
Resolve the script's path first — a bare relative path resolves against the user's
working directory, not the plugin, and Codex sets no plugin-root variable at all:
```

to

```
Resolve the script's path first — a bare relative path resolves against the user's
working directory, not the plugin. Codex sets no plugin-root variable at all, and
opencode installs packages under `~/.cache/opencode/packages`, so the fallback
searches every harness's install location:
```

and change the `find` invocation

```bash
[ -f "$AUDIT" ] || AUDIT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents -name audit_rls.py 2>/dev/null \
    | xargs -r ls -t | head -1
)
```

to

```bash
[ -f "$AUDIT" ] || AUDIT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents ~/.cache/opencode \
    -name audit_rls.py 2>/dev/null | xargs -r ls -t | head -1
)
```

- [ ] **Step 3: Extend the pr-review fallback**

In `pr-review/skills/pr-review/SKILL.md`, change the prose sentence

```
`CLAUDE_PLUGIN_ROOT` is set by Claude Code only — **Codex sets no plugin-root variable
at all**, so it would expand to an empty string. Resolve the root once, then reuse it:
```

to

```
`CLAUDE_PLUGIN_ROOT` is set by Claude Code only — **Codex sets no plugin-root variable
at all**, so it would expand to an empty string, and opencode uses neither. Resolve the
root once, then reuse it:
```

and change the `find` invocation

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -f "$PLUGIN_ROOT/scripts/build_prompts.py" ] || PLUGIN_ROOT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents -path "*pr-review*" \
       -name build_prompts.py 2>/dev/null | head -1 | xargs -r dirname | xargs -r dirname
)
```

to

```bash
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
[ -f "$PLUGIN_ROOT/scripts/build_prompts.py" ] || PLUGIN_ROOT=$(
  find ~/.claude/plugins ~/.codex/plugins ~/.agents ~/.cache/opencode \
       -path "*pr-review*" -name build_prompts.py 2>/dev/null \
       | head -1 | xargs -r dirname | xargs -r dirname
)
```

- [ ] **Step 4: Verify no unextended fallbacks remain**

Run: `rg -n 'find ~/.claude/plugins ~/.codex/plugins ~/.agents -' --glob '!docs/**'`
Expected: no output.

- [ ] **Step 5: Verify the skills still validate**

Run: `uv run scripts/validate_skills.py && uv run scripts/sync_manifests.py --check`
Expected: both pass (only prose changed, frontmatter untouched).

- [ ] **Step 6: Commit**

```bash
git add supabase-security/skills/supabase-security/SKILL.md pr-review/skills/pr-review/SKILL.md
git commit -m "fix: let plugin-root fallbacks find opencode's package cache

The self-locating fallbacks searched ~/.claude/plugins, ~/.codex/plugins,
and ~/.agents, so under opencode (which installs git plugin specs into
~/.cache/opencode/packages) the audit and prompt-builder scripts were only
found when another harness happened to have the plugin installed."
```

---

### Task 4: Document opencode as a supported harness

**Files:**
- Create: `.opencode/INSTALL.md`
- Modify: `README.md`
- Modify: `AGENTS.md` (`CLAUDE.md` is a symlink to it; verify with `ls -l CLAUDE.md`)

**Interfaces:**
- Consumes: the install spec string `yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git` (must match `PACKAGE_NAME` and the repository URL).
- Produces: user-facing install and update instructions.

- [ ] **Step 1: Write `.opencode/INSTALL.md`**

```markdown
# Installing agent-skills for opencode

## Prerequisites

- [opencode](https://opencode.ai) installed

## Installation

Add the plugin to the `plugin` array in your `opencode.json` (global or project-level):

```json
{
  "plugin": ["yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git"]
}
```

Restart opencode. The plugin installs through opencode's plugin manager and registers
every plugin's skills and commands.

Verify by asking "list your skills" (you should see `agent-session-monitor`, `flowchart`,
`pr-review`, `reflect`, `supabase-security`, `task-status`, and `visual-design-review`)
or by typing `/dev-loop`.

opencode uses its own plugin install. If you also use Claude Code or Codex, install this
repository separately for each harness.

## What gets registered

- Skills from every plugin that has a `skills/` directory.
- Commands from every plugin that has a `commands/` directory (`dev-loop`,
  `review-loop`, and `workflow`).
- Not registered: `self-improve-skill`'s session hooks, which have no opencode
  equivalent yet.

A skill or command you defined yourself wins over the plugin's version.

## Updating

opencode installs through a git-backed package spec. Some opencode and Bun versions pin
the resolved git dependency in a lockfile or cache, so a restart may not pick up the
newest commit. If updates do not appear, remove the cached package and restart:

```bash
rm -rf ~/.cache/opencode/packages/yorrick-agent-skills*
```

No git tags exist yet, so installs track `main`.

## Troubleshooting

### Plugin not loading

1. Check the logs: `opencode run --print-logs "hello" 2>&1 | grep -i agent-skills`
2. Verify the plugin line in your `opencode.json`
3. Make sure you are running a recent version of opencode

### Skills not found

Use opencode's `skill` tool to list what was discovered.
```

- [ ] **Step 2: Update the README intro and install section**

In `README.md`, change line 3

```
A collection of Claude Code plugins by Yorrick Jansen.
```

to

```
A collection of agent plugins by Yorrick Jansen (Claude Code, Codex, and opencode).
```

change line 7

```
Every plugin here works with **both Claude Code and Codex**.
```

to

```
Every plugin here works with **Claude Code, Codex, and opencode**.
```

and after the Codex install block (the fenced block ending with `codex plugin add <plugin-name>@yorrick`) and before `Restart the CLI afterwards`, insert:

````
**opencode**

```json
{
  "plugin": ["yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git"]
}
```

See [`.opencode/INSTALL.md`](.opencode/INSTALL.md) for the update story and caveats.
````

- [ ] **Step 3: Update the README updating/removing sections**

In the `### Updating` section of `README.md`, after the `**Codex auto-update is a supply-chain path.**` paragraph, add:

```
opencode installs the repository as a git plugin spec and may pin the resolved commit
in a lockfile, so a restart alone may not fetch a newer revision. If changes do not
appear, remove the cached package and restart:

```bash
rm -rf ~/.cache/opencode/packages/yorrick-agent-skills*
```
```

In the `### Removing` section, after the Codex line, add:

```
# opencode — remove the plugin line from opencode.json, then clear the cache above
```

In the `## Local Development` section, after the existing Claude `--plugin-dir` block, add:

````
opencode resolves a filesystem path as a plugin spec, so a checkout can be loaded
directly:

```json
{
  "plugin": ["/path/to/agent-skills"]
}
```
````

- [ ] **Step 4: Update `AGENTS.md`**

Change line 3

```
Agent skills and plugins for **both Claude Code and Codex**.
```

to

```
Agent skills and plugins for **Claude Code, Codex, and opencode**.
```

Change the sentence in "The rule that matters"

```
The JSON manifests are **generated** from each plugin's
`plugin.toml`; never hand-edit a `plugin.json` or `marketplace.json`.
```

to

```
The JSON manifests and the root `package.json` are **generated** from each plugin's
`plugin.toml`; never hand-edit a `plugin.json`, `marketplace.json`, or `package.json`.
```

After the "Marketplace manifests" paragraph, add:

```
opencode has no manifest. Its install surface is `package.json` (generated) plus
`.opencode/plugins/agent-skills.js` (hand-written), a plugin entry that discovers every
`plugin.toml` directory at load time and registers its `skills/` and `commands/` with
opencode's config. The same file also works as a project plugin when opencode runs
inside a checkout of this repository.
```

In the `## Installing` section, after the Codex block, add:

````
**opencode**

```json
{
  "plugin": ["yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git"]
}
```
````

In the `## Validation checklist (every change)` section, change

```
- **Contract tests**: `uv run pytest tests/` must pass when `tests/` exists.
```

to

```
- **Contract tests**: `uv run pytest tests/` must pass when `tests/` exists. This
  includes the opencode entry test, which runs `node`.
```

- [ ] **Step 5: Verify the docs gates**

Run: `uv run scripts/sync_manifests.py --check && uv run scripts/validate_skills.py && ls -l CLAUDE.md`
Expected: checks pass; `CLAUDE.md` confirms it is a symlink to `AGENTS.md`.

- [ ] **Step 6: Commit**

```bash
git add .opencode/INSTALL.md README.md AGENTS.md
git commit -m "docs: document opencode as a third supported harness

Adds the one-line install spec, the update caveat (opencode may pin the
git revision in a cache), the filesystem-path trick for local
development, and what is and is not registered (session hooks are not)."
```

---

### Task 5: Full validation, end-to-end check in real opencode, and cross-AI review

**Files:**
- No new files; this task verifies the finished change.

**Interfaces:**
- Consumes: everything above.
- Produces: the verification evidence quoted in the pull request.

- [ ] **Step 1: Run every repository gate**

Run:

```bash
uv run scripts/sync_manifests.py --check
uv run scripts/validate_skills.py
uv run pytest tests/ -v
uv run ruff format --check .
uv run ruff check scripts/ tests/
uv run pyright
```

Expected: all pass; the opencode test shows 8 passed (plus the pre-existing tests).

- [ ] **Step 2: End-to-end in real opencode, before any merge**

Create a scratch project directory that points opencode at this checkout:

```bash
mkdir -p /tmp/agentskills-opencode-e2e
cat > /tmp/agentskills-opencode-e2e/opencode.json <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["/Users/yorrickjansen/work/agent-skills"]
}
EOF
```

Then run, from that directory:

```bash
cd /tmp/agentskills-opencode-e2e && opencode run "From your available skills, list every skill name you can see. Comma separated, nothing else." 2>&1 | tail -3
```

Expected: the output includes `agent-session-monitor`, `flowchart`, `pr-review`,
`reflect`, `supabase-security`, `task-status`, and `visual-design-review`.

If opencode does not accept a filesystem path as a plugin spec, fall back to
verifying after merge via the git spec; record which path was used.

- [ ] **Step 3: Verify the commands registered in real opencode**

Run, from the scratch directory:

```bash
cd /tmp/agentskills-opencode-e2e && opencode run --print-logs "hello" 2>&1 | grep -iE 'agent-skills|command' | head -10
```

Expected: log lines showing the plugin loading without errors. The definitive
check of the command menu is interactive, so also report that a full template
substitution was verified in Step 1 by the `review-loop` assertion.

- [ ] **Step 4: Confirm the Claude Code marketplace path still works**

Run:

```bash
claude plugin marketplace update yorrick
```

Expected: the command succeeds (exit code 0). If it reports that the marketplace is
not registered, note that in the PR instead of registering it.

- [ ] **Step 5: Cross-AI review by Claude Code**

Write a payload that contains the diff and the spec reference, then run:

```bash
git diff origin/main...HEAD > /tmp/opencode-harness-review.diff
{
  echo "Review this implementation against its design spec. The repo is /Users/yorrickjansen/work/agent-skills on branch feat/opencode-harness; the spec is docs/superpowers/specs/2026-09-16-opencode-harness-design.md and the plan is its sibling under docs/superpowers/plans/. You are a leaf reviewer: do not invoke another AI CLI. Report findings as a prioritized list with file:line, and say explicitly if there are no blocking issues."
  echo
  echo '```diff'
  cat /tmp/opencode-harness-review.diff
  echo '```'
} > /tmp/opencode-harness-review-payload.md
claude -p --model opus < /tmp/opencode-harness-review-payload.md > /tmp/opencode-harness-review-findings.md
```

(Use the highest-capability Claude model available, and allow as long as it
needs; never downgrade because of a timeout.)

- [ ] **Step 6: Triage the review findings**

Treat findings as claims to verify, not instructions. Check each against the code,
fix the valid ones, and reply in the PR or commit message with what was accepted and
what was rejected and why. Re-run Step 1 after any fix.

- [ ] **Step 7: Push the branch and open the pull request**

```bash
git push origin HEAD
gh pr create --title "Add opencode as a supported harness" --body "$(cat <<'EOF'
Adds a single opencode plugin spec that registers every plugin's skills and commands, a generated root package.json, a contract test that runs the entry under node, and install docs. Nothing changes for Claude Code or Codex, apart from plugin-root fallbacks that now also search opencode's package cache.
EOF
)"
```

Report the PR URL. Do not merge.

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Decisions 1-5 | Tasks 1, 2 (entry behaviour, naming, skip-if-exists) |
| Architecture: new files | Tasks 1, 2, 4 |
| Architecture: load path and discovery invariant | Task 2 Step 4 |
| Generated package.json | Task 1 |
| Entry behaviour (skills, commands, substitution, skip-if-exists) | Task 2 |
| Shared skill fix | Task 3 |
| Documentation | Task 4 |
| CI test | Task 2 Steps 1-5 |
| Edge cases | Covered by Task 2 implementation and its tests |
| Testing and verification (CI, manual, marketplace regression) | Task 5 Steps 1-4 |
| Rollout | Task 5 Step 7 |

**Placeholder scan:** no TBDs; every code and prose block is complete and
copy-pasteable.

**Type consistency:** the harness emits `exports`, `config`, `configAfterSecondPass`,
`seeded`; the test reads exactly those keys. `package_json`/`version_tuple`/
`PACKAGE_NAME` names are used consistently between Task 1 Steps 2-4. The install spec
string `yorrick-agent-skills@git+https://github.com/yorrick/agent-skills.git` matches
`PACKAGE_NAME` plus `REPOSITORY_URL` in both Task 4 destinations.
