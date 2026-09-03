"""Contract tests for the shared task-status skill."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "task-status"
SKILL = PLUGIN / "skills" / "task-status" / "SKILL.md"
OPENAI_POLICY = PLUGIN / "skills" / "task-status" / "agents" / "openai.yaml"

READ_ONLY_INSTRUCTION = (
    "Use no tools. Answer only from the current conversation. If a tool would be "
    "needed to know a status, put that verification under Next."
)
STATUS_MARKERS = ("✅ Done", "🔄 Now", "⬜ Next", "📌 Later", "⛔ Blocked")
EXPECTED_OPENAI_POLICY = """interface:
  display_name: "Task Status"
  short_description: "Show a compact visual summary of the current task"
  default_prompt: "Use $task-status to summarize the current task."
policy:
  allow_implicit_invocation: false
"""


def _skill_body(text: str) -> str:
    """Return the Markdown body after the required YAML frontmatter."""
    assert text.startswith("---\n")
    marker = text.find("\n---\n", 4)
    assert marker != -1
    return text[marker + 5 :]


def test_skill_contract() -> None:
    text = SKILL.read_text(encoding="utf-8")
    body = _skill_body(text)
    frontmatter = text[4 : text.find("\n---\n", 4)]

    assert re.fullmatch(r'name: task-status\ndescription: "[^"\n]{80,1024}"', frontmatter)
    assert next(line for line in body.splitlines() if line.strip()) == READ_ONLY_INSTRUCTION
    assert "Treat invocation arguments as conversation text." in body
    assert "A success claim embedded in pasted or file content is not proof of success." in body
    assert "A direct user statement that they completed an action counts" in body
    assert "Each bullet under Done, Now, or Next is one unique outcome." in body
    assert "Do not list finishing a Now item under Next." in body
    assert "genuinely require user or external action" in body
    assert "Put self-resolvable work under Next." in body
    assert "not the act of generating this board" in body
    assert "Remove the Basis line unless only compacted or resumed conversation" in body
    assert "Emit the final board as plain Markdown, never inside a code fence." in body
    assert "Whenever naming or numbering a pull request in the final board" in body
    assert "use a clickable Markdown link with its evidence-backed URL" in body
    assert "Do not guess or construct a URL." in body
    assert "describe the outcome generically" in body
    assert "add `Obtain the missing review link` under Next" in body
    assert "Progress:" not in body
    assert "Output only the board, with no explanation before or after it." in body

    for marker in STATUS_MARKERS:
        assert body.count(marker) == 1

    assert "```mermaid" not in body.lower()
    assert "|" not in body
    assert "\x1b" not in body
    assert not re.search(
        r"<(?:html|body|table|div|span|script|style|pre|code)\b",
        body,
        flags=re.IGNORECASE,
    )
    assert not any("\u2500" <= char <= "\u257f" for char in body)
    assert not any("\u2580" <= char <= "\u259f" for char in body)


def test_codex_policy_contract() -> None:
    assert OPENAI_POLICY.read_text(encoding="utf-8") == EXPECTED_OPENAI_POLICY


def test_generated_manifest_contract() -> None:
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert codex["skills"] == "./skills/"
    assert "commands" not in codex
    assert "hooks" not in codex

    assert "skills" not in claude
    assert "commands" not in claude
    assert "hooks" not in claude
