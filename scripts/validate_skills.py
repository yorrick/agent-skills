#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Validate every SKILL.md against what both harnesses require.

Claude Code and Codex read the same SKILL.md format, so one check covers both.
A skill that fails here loads incorrectly, or silently never loads at all --
which is worse, because nothing reports an error.

Usage:
    uv run scripts/validate_skills.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Reason: plugins live at the repo root here, not under plugins/.
PLUGINS = REPO

# Reason: an agent decides whether to load a skill from its description alone.
# Too short and it never fires; the harnesses also truncate very long ones.
MIN_DESCRIPTION = 80
MAX_DESCRIPTION = 1024


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Extract YAML frontmatter without a yaml dependency.

    Only flat `key: value` pairs are supported, which is all a SKILL.md header
    needs -- keeping this stdlib-only means CI needs no install step.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None

    out: dict[str, str] = {}
    key: str | None = None
    for raw in text[4:end].splitlines():
        if not raw.strip():
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", raw)
        if m:
            found: str = m.group(1)
            value: str = m.group(2).strip()
            # Reason: descriptions are long and routinely quoted; strip one
            # matching pair of surrounding quotes rather than mangling them.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[found] = value
            key = found
        elif key is not None and raw.startswith((" ", "\t")):
            # Continuation of a folded value.
            out[key] = f"{out[key]} {raw.strip()}".strip()
    return out


def validate(skill_md: Path) -> list[str]:
    """Return a list of problems; empty means the skill is valid."""
    rel = skill_md.relative_to(REPO)
    problems: list[str] = []
    text = skill_md.read_text()

    meta = parse_frontmatter(text)
    if meta is None:
        return [f"{rel}: no YAML frontmatter (must start with '---')"]

    for field in ("name", "description"):
        if not meta.get(field):
            problems.append(f"{rel}: frontmatter is missing required '{field}'")

    name = meta.get("name", "")
    if name and name != skill_md.parent.name:
        problems.append(f"{rel}: name '{name}' does not match directory '{skill_md.parent.name}'")
    if name and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        problems.append(f"{rel}: name '{name}' must be lowercase-hyphenated")

    desc = meta.get("description", "")
    if desc:
        if len(desc) < MIN_DESCRIPTION:
            problems.append(
                f"{rel}: description is {len(desc)} chars; under {MIN_DESCRIPTION} "
                "an agent cannot tell when to load the skill"
            )
        if len(desc) > MAX_DESCRIPTION:
            problems.append(f"{rel}: description is {len(desc)} chars, over the {MAX_DESCRIPTION} limit")

    # Reason: a reference that does not exist is worse than none -- the agent
    # burns a tool call discovering the file is missing, then proceeds without it.
    for ref in re.findall(r"`references/([\w./-]+)`", text):
        if not (skill_md.parent / "references" / ref).exists():
            problems.append(f"{rel}: references/{ref} is cited but does not exist")

    return problems


def main() -> int:
    skills = sorted(PLUGINS.glob("*/skills/*/SKILL.md"))
    if not skills:
        print("No SKILL.md found", file=sys.stderr)
        return 1

    problems: list[str] = []
    for skill in skills:
        problems.extend(validate(skill))

    if problems:
        print(f"{len(problems)} problem(s) found:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    print(f"All {len(skills)} skill(s) valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
