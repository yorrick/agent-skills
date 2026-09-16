#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Generate every harness-specific manifest from one source of truth.

Write the skill ONCE. Each plugin declares itself in `plugins/<name>/plugin.toml`;
this script derives all five generated files from it:

    .claude-plugin/plugin.json          per plugin, Claude Code
    .codex-plugin/plugin.json           per plugin, Codex
    .claude-plugin/marketplace.json     repo root, Claude Code (Codex reads it too)
    .agents/plugins/marketplace.json    repo root, Codex canonical
    package.json                        repo root, opencode

Nothing else differs between the harnesses: SKILL.md, references/ and scripts/ are
shared verbatim, because all three CLIs consume the same SKILL.md format.

opencode has no manifest of its own: it installs this repository as a git package
and resolves package.json's `main` to .opencode/plugins/agent-skills.js, a
hand-written entry that registers every plugin's skills and commands at load time.

Usage:
    uv run scripts/sync_manifests.py            # write the manifests
    uv run scripts/sync_manifests.py --check    # verify they are current (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Reason: plugins live at the repo root here rather than under plugins/, because
# that is where they already were before Codex support was added and moving them
# would break every existing install path.
PLUGINS = REPO

MARKETPLACE_NAME = "yorrick"
OWNER = "Yorrick Jansen"
MARKETPLACE_DESCRIPTION = "Agent skills and plugins by Yorrick Jansen (Claude Code, Codex, and opencode)"
REPOSITORY_URL = "https://github.com/yorrick/agent-skills"
PACKAGE_NAME = "yorrick-agent-skills"


def load_plugins() -> list[dict]:
    """Read every <plugin>/plugin.toml. This is the ONLY hand-edited metadata.

    A directory is a plugin if and only if it contains plugin.toml -- so docs/,
    scripts/ and any other top-level directory are skipped without needing a
    hard-coded exclusion list that would rot.
    """
    out: list[dict] = []
    for spec in sorted(PLUGINS.glob("*/plugin.toml")):
        meta = tomllib.loads(spec.read_text())["plugin"]
        missing = {"name", "version", "description"} - meta.keys()
        if missing:
            raise SystemExit(f"{spec}: missing required key(s): {', '.join(sorted(missing))}")
        if meta["name"] != spec.parent.name:
            raise SystemExit(f"{spec}: name '{meta['name']}' does not match directory '{spec.parent.name}'")
        out.append(meta)
    if not out:
        raise SystemExit(f"no plugins found under {PLUGINS}")
    return out


def plugin_manifest(meta: dict, *, harness: str) -> dict:
    """The per-plugin manifest.

    The metadata is identical for both harnesses; the COMPONENT PATHS are not.

    Codex's manifest loader has `skills` as Option, and resolve_manifest_paths
    maps None to an empty vec (codex-rs/core-plugins/src/manifest.rs). In practice
    Codex ALSO falls back to convention-based discovery -- tested by deleting the
    field and confirming the skill still appeared in `codex exec "list your
    skills"` -- so omitting it is not currently fatal.

    We declare the paths anyway: it is what OpenAI's own bundled manifests do, it
    does not depend on undocumented fallback behaviour, and it is the only way
    dev-loop's commands/ gets declared at all.

    Claude Code discovers skills/ and commands/ by convention and needs none of it.
    """
    manifest = {
        "name": meta["name"],
        "description": meta["description"],
        "version": meta["version"],
        "author": {"name": OWNER},
        "repository": REPOSITORY_URL,
        "license": meta.get("license", "UNLICENSED"),
    }
    if keywords := meta.get("keywords"):
        manifest["keywords"] = keywords

    if harness == "codex":
        # Reason: declare only components that actually exist, so the manifest
        # never points at a missing directory.
        root = PLUGINS / meta["name"]
        if (root / "skills").is_dir():
            manifest["skills"] = "./skills/"
        if (root / "commands").is_dir():
            manifest["commands"] = "./commands/"
        if (root / "hooks").is_dir():
            manifest["hooks"] = "./hooks/"
    return manifest


def marketplace_manifest(plugins: list[dict]) -> dict:
    return {
        "name": MARKETPLACE_NAME,
        "owner": {"name": OWNER},
        "metadata": {"description": MARKETPLACE_DESCRIPTION},
        "plugins": [
            {
                "name": p["name"],
                "source": f"./{p['name']}",
                "description": p["description"],
                "version": p["version"],
                "author": {"name": OWNER},
            }
            for p in plugins
        ],
    }


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


def targets(plugins: list[dict]) -> dict[Path, dict]:
    """Every generated file, mapped to its expected content."""
    out: dict[Path, dict] = {}
    for p in plugins:
        base = PLUGINS / p["name"]
        out[base / ".claude-plugin" / "plugin.json"] = plugin_manifest(p, harness="claude")
        out[base / ".codex-plugin" / "plugin.json"] = plugin_manifest(p, harness="codex")
    market = marketplace_manifest(plugins)
    out[REPO / ".claude-plugin" / "marketplace.json"] = market
    out[REPO / ".agents" / "plugins" / "marketplace.json"] = market
    out[REPO / "package.json"] = package_json(plugins)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        action="store_true",
        help="verify manifests are current; write nothing",
    )
    args = ap.parse_args()

    expected = targets(load_plugins())
    stale: list[Path] = []

    for path, content in expected.items():
        rendered = json.dumps(content, indent=2) + "\n"
        current = path.read_text() if path.exists() else None
        if current == rendered:
            continue
        stale.append(path.relative_to(REPO))
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered)

    if args.check:
        if stale:
            print(
                "Manifests are out of date. Run: uv run scripts/sync_manifests.py",
                file=sys.stderr,
            )
            for p in stale:
                print(f"  {p}", file=sys.stderr)
            return 1
        print(f"All {len(expected)} manifests current.")
        return 0

    if stale:
        print(f"Wrote {len(stale)} manifest(s):")
        for p in stale:
            print(f"  {p}")
    else:
        print(f"All {len(expected)} manifests already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
