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
