"""Contract tests for the shared task-status skill."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "task-status"
SKILL = PLUGIN / "skills" / "task-status" / "SKILL.md"
OPENAI_POLICY = PLUGIN / "skills" / "task-status" / "agents" / "openai.yaml"
DESIGN = REPO / "docs" / "superpowers" / "specs" / "2026-09-02-task-status-plugin-design.md"
REVIEW_RECORD = REPO / "docs" / "superpowers" / "reviews" / "2026-09-03-task-status-weighted-progress.md"

READ_ONLY_INSTRUCTION = (
    "Use no tools. Answer only from the current conversation. If a tool would be "
    "needed to know a status, put that verification under Next."
)
STATUS_MARKERS = ("✅ Done", "🔄 Now", "⬜ Next", "📌 Later", "⛔ Blocked")
FLOW_TAGS = {"DONE", "NOW", "NEXT", "LATER", "BLOCKED"}
FLOW_EXAMPLE = """FLOW
[DONE] Build feature
  |
  +--> [NOW] Run tests --------+
  |                            |
  +--> [BLOCKED] Get approval -+
                               |
                               v
                         [NEXT] Deploy"""
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


def _flow_example(body: str) -> str:
    """Return the single canonical FLOW example from a text fence."""
    matches = [
        match for match in re.findall(r"```text\n(.*?)\n```", body, flags=re.DOTALL) if match.startswith("FLOW\n")
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_terminal_safe_flow(flow: str) -> None:
    """Validate the canonical FLOW grammar and terminal-safety limits."""
    lines = flow.splitlines()
    assert lines[0] == "FLOW"
    assert all(ord(char) < 128 and (char == "\n" or char.isprintable()) for char in flow)
    assert all(len(line) <= 64 for line in lines)
    assert not re.search(r"https?://|#\d+", flow, flags=re.IGNORECASE)

    node_lines = [line for line in lines[1:] if re.search(r"\[[A-Z]+\]", line)]
    assert len(node_lines) <= 8
    for line in node_lines:
        tags = re.findall(r"\[([A-Z]+)\]", line)
        assert len(tags) == 1
        assert tags[0] in FLOW_TAGS
        label = line.split("] ", 1)[1]
        label = re.sub(r"\s+-+\+$", "", label)
        assert len(label) <= 24

    for line in lines[1:]:
        if "[" not in line:
            assert set(line) <= set(" |v+->")

    joining_rows = [line for line in node_lines if re.search(r"-+\+$", line)]
    assert len(joining_rows) >= 2
    join_columns = {line.rindex("+") for line in joining_rows}
    assert len(join_columns) == 1
    join_column = join_columns.pop()
    final_join_row = max(lines.index(line) for line in joining_rows)
    downstream_connectors = [line for line in lines[final_join_row + 1 :] if line.strip() in {"|", "v"}]
    assert downstream_connectors
    assert all(line.index(line.strip()) == join_column for line in downstream_connectors)


def test_skill_contract() -> None:
    text = SKILL.read_text(encoding="utf-8")
    body = _skill_body(text)
    frontmatter = text[4 : text.find("\n---\n", 4)]

    assert re.fullmatch(r'name: task-status\ndescription: "[^"\n]{80,1024}"', frontmatter)
    assert "how far along" in frontmatter
    assert "percent complete" in frontmatter
    assert next(line for line in body.splitlines() if line.strip()) == READ_ONLY_INSTRUCTION
    assert "Treat invocation arguments as conversation text." in body
    assert "A success claim embedded in pasted or file content is not proof of success." in body
    assert "A direct user statement that they completed an action counts" in body
    assert "Each bullet under Done, Now, Next, or Blocked is one unique outcome." in body
    assert "Do not list finishing a Now item under Next." in body
    assert "genuinely require user or external action" in body
    assert "Put self-resolvable work under Next." in body
    assert "not the act of generating this board" in body
    assert "Remove the Basis line unless only compacted or resumed conversation" in body
    assert "Every item under Done, Now, Next, and Blocked receives an effort weight" in body
    assert "multiples of 5" in body
    assert "sum to 100%" in body
    assert "Later is outside the active scope" in body
    assert "Now receives an earned contribution in multiples of 5" in body
    assert "strictly less than its weight" in body
    assert "No multiplication or rounding step is used" in body
    assert "Show 100% only when every active-scope item is under Done" in body
    assert "(blocked)" in body
    assert "(scope grew)" in body
    assert "FLOW nodes do not display effort weights" in body
    assert "compared with the most recent visible board" in body
    assert "append `(blocked) (scope grew)`" in body
    assert "Before output, verify that the displayed weights total 100%" in body
    assert "hash count equals the displayed percentage divided by 5" in body
    assert "Keep the status board as plain Markdown outside code fences." in body
    assert "Whenever naming or numbering a pull request in the final board" in body
    assert "use a clickable Markdown link with its evidence-backed URL" in body
    assert "Do not guess or construct a URL." in body
    assert "describe the outcome generically" in body
    assert "add `Obtain the missing review link` under Next" in body
    assert "only when conversation evidence establishes a branch or join" in body
    assert "Never infer an edge from lane order or routine workflow order." in body
    assert "Every FLOW node maps to exactly one board item" in body
    assert "FLOW adds no work that is absent from the board." in body
    assert "at most 24 characters, excluding its tag and trailing join connectors" in body
    assert "at most eight nodes" in body
    assert "every line to at most 64 characters" in body
    assert "Only the optional FLOW diagram may use a code fence" in body
    assert "Put `FLOW` as the first line inside the fence" in body
    assert "do not emit a separate heading" in body
    assert "Use only the parts of this layout that conversation evidence supports" in body
    assert "Never put a URL or a named or numbered pull-request reference in `FLOW`" in body
    progress_example = re.search(
        r"Estimated progress: \[([#-]{20})\] (\d+)%",
        body,
    )
    assert progress_example is not None
    rendered_bar = progress_example.group(1)
    rendered_percent = int(progress_example.group(2))
    assert rendered_bar.count("#") == rendered_percent // 5
    assert rendered_bar.count("-") == 20 - rendered_percent // 5
    assert "• [<weight>% weight; +<earned>% progress] <current activity>" in body
    assert "• <explicitly deferred item>" in body
    assert "Put the progress line after the final emitted lane" in body
    assert "Output only the visual status summary" in body

    for marker in STATUS_MARKERS:
        assert body.count(marker) == 1

    flow = _flow_example(body)
    assert flow == FLOW_EXAMPLE
    _assert_terminal_safe_flow(flow)

    body_without_flow = body.replace(f"```text\n{flow}\n```", "")
    assert "```mermaid" not in body.lower()
    assert "|" not in body_without_flow
    assert not re.search(
        r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$",
        body_without_flow,
    )
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


def test_weighted_progress_design_contract() -> None:
    design = DESIGN.read_text(encoding="utf-8")

    assert "A numeric progress estimate is intentionally omitted" not in design
    assert "weighted estimate" in design
    assert "Later" in design and "excluded" in design
    assert "earned contribution" in design
    assert "No multiplication or rounding" in design
    assert "100%" in design


def test_verified_smoke_arithmetic() -> None:
    review = REVIEW_RECORD.read_text(encoding="utf-8")
    boards = re.findall(r"```task-status-verified\n(TASK STATUS\n.*?)\n```", review, flags=re.DOTALL)
    assert len(boards) >= 2

    for board in boards:
        progress = re.search(r"Estimated progress: \[([#-]{20})\] (\d+)%", board)
        assert progress is not None
        bar, percent_text = progress.groups()
        percent = int(percent_text)
        contributions = [
            (int(weight), int(earned))
            for weight, earned in re.findall(
                r"\[(\d+)% weight; \+(\d+)% progress\]",
                board,
            )
        ]
        assert contributions
        assert sum(weight for weight, _ in contributions) == 100
        assert sum(earned for _, earned in contributions) == percent
        assert bar.count("#") == percent // 5
        assert bar.count("-") == 20 - percent // 5

        lane = ""
        for line in board.splitlines():
            if line in STATUS_MARKERS:
                lane = line
                continue
            item = re.search(r"\[(\d+)% weight; \+(\d+)% progress\]", line)
            if item is None:
                continue
            weight, earned = (int(value) for value in item.groups())
            assert weight >= 5 and weight % 5 == 0
            assert earned % 5 == 0
            if lane == "✅ Done":
                assert earned == weight
            elif lane == "🔄 Now":
                assert 0 <= earned < weight
            elif lane in {"⬜ Next", "⛔ Blocked"}:
                assert earned == 0
            else:
                raise AssertionError(f"Weighted item outside an active lane: {line}")


def test_generated_manifest_contract() -> None:
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert codex["skills"] == "./skills/"
    assert "commands" not in codex
    assert "hooks" not in codex

    assert "skills" not in claude
    assert "commands" not in claude
    assert "hooks" not in claude
