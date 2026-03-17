# Workflow Generation Eval Framework — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a scenario-based eval framework that tests whether the `/workflow` skill generates structurally sound workflows by importing and inspecting the generated StateGraph with Python assertions.

**Architecture:** Scenarios are self-contained directories with a repo scaffold, a TOML config (name + prompt), and an assertions module. A pytest runner discovers scenarios, calls headless Claude with the dev-loop plugin installed via `--plugin-dir` to generate a workflow script, imports the graph via `build_graph()`, and runs the assertions through a `GraphInspector` helper.

**Tech Stack:** Python 3.12, pytest, tomllib (stdlib), StateGraph engine, Claude CLI (`claude -p --plugin-dir`)

**Spec:** `docs/superpowers/specs/2026-03-17-workflow-eval-framework-design.md`

---

### Task 1: Add `_prompt_template` attribute to engine node helpers

**Files:**
- Modify: `dev-loop/scripts/engine.py:419-543`
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write tests for `_prompt_template` on node helpers**

Add to `dev-loop/tests/test_engine.py`:

```python
def test_claude_node_has_prompt_template():
    """claude_node exposes its prompt template as _prompt_template."""
    node = claude_node("Fix this: {error}", output_key="fix")
    assert node._prompt_template == "Fix this: {error}"


def test_codex_node_has_prompt_template():
    """codex_node exposes its prompt template as _prompt_template."""
    node = codex_node("Implement: {spec}", output_key="code")
    assert node._prompt_template == "Implement: {spec}"


def test_gemini_node_has_prompt_template():
    """gemini_node exposes its prompt template as _prompt_template."""
    node = gemini_node("Summarize: {findings}", output_key="summary")
    assert node._prompt_template == "Summarize: {findings}"


def test_shell_node_has_prompt_template():
    """shell_node exposes its command template as _prompt_template."""
    node = shell_node("uv run pytest {test_path}", output_key="test_out")
    assert node._prompt_template == "uv run pytest {test_path}"


def test_template_node_has_prompt_template():
    """template_node exposes its template string as _prompt_template."""
    node = template_node("Report: {lint}\n{tests}", output_key="report")
    assert node._prompt_template == "Report: {lint}\n{tests}"


def test_template_node_has_diagram_label():
    """template_node has a _diagram_label."""
    node = template_node("Report: {output}", output_key="report")
    assert node._diagram_label == "template"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest dev-loop/tests/test_engine.py -k "prompt_template or template_node_has_diagram" -v`
Expected: 6 FAILs (attributes don't exist yet)

- [ ] **Step 3: Add `_prompt_template` to all node helpers and `_diagram_label` to `template_node`**

In `dev-loop/scripts/engine.py`:

For `claude_node` (after line 436):
```python
_node._prompt_template = prompt_template  # type: ignore[attr-defined]
```

For `codex_node` (after line 461):
```python
_node._prompt_template = prompt_template  # type: ignore[attr-defined]
```

For `gemini_node` (after line 477):
```python
_node._prompt_template = prompt_template  # type: ignore[attr-defined]
```

For `shell_node` (after line 533):
```python
_node._prompt_template = command_template  # type: ignore[attr-defined]
```

For `template_node` (after line 541, before `return _node`):
```python
_node._diagram_label = "template"  # type: ignore[attr-defined]
_node._prompt_template = template  # type: ignore[attr-defined]
```

Also add `codex_node` to the import in `test_engine.py` if not already imported, and add `gemini_node`:
```python
from engine import (
    ...,
    codex_node,
    gemini_node,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest dev-loop/tests/test_engine.py -k "prompt_template or template_node_has_diagram" -v`
Expected: 6 PASSes

- [ ] **Step 5: Run full engine test suite**

Run: `uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All tests pass (no regressions)

- [ ] **Step 6: Run linting and type checking**

Run: `uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add dev-loop/scripts/engine.py dev-loop/tests/test_engine.py
git commit -m "feat(engine): add _prompt_template attribute to node helpers

Expose the prompt/command template string on claude_node, codex_node,
gemini_node, shell_node, and template_node. Also add _diagram_label
to template_node. Enables structural inspection of generated workflows."
```

---

### Task 2: Update workflow skill to require `build_graph()` function

**Files:**
- Modify: `dev-loop/commands/workflow.md:28-66`

- [ ] **Step 1: Update the script template in workflow.md**

Replace the script template section (lines 28-66) to use `build_graph()`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mermaid-ascii"]
# ///
import os
import sys
sys.path.insert(0, "${CLAUDE_PLUGIN_ROOT}/scripts")

import asyncio
from engine import (
    StateGraph, claude_node, codex_node, gemini_node,
    shell_node, python_node, template_node,
    detect_available_models, END,
)


def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    """Build the workflow graph. Accepts optional models dict for testing."""
    if models is None:
        models = detect_available_models()
    HAS_CODEX = models["codex"]
    HAS_GEMINI = models["gemini"]

    graph = StateGraph(max_iterations=5)

    # ... define nodes and edges ...
    # Use codex_node/gemini_node when available and appropriate,
    # fall back to claude_node otherwise. See model selection guide below.

    return graph


if __name__ == "__main__":
    graph = build_graph()
    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        sys.exit(0)
    initial_state = {"work_dir": os.getcwd()}
    asyncio.run(graph.run(initial_state))
```

- [ ] **Step 2: Update example 1 (test-fix loop) to use `build_graph()`**

Wrap the graph construction in a `build_graph(models=None)` function. Keep the nodes/edges exactly the same, just move them inside the function and add the `return graph` + `if __name__` boilerplate.

- [ ] **Step 3: Update example 2 (parallel lint+typecheck+test) to use `build_graph()`**

Same pattern: wrap in `build_graph()`, return graph, add `if __name__` block.

- [ ] **Step 4: Update example 3 (implement→review→commit) to use `build_graph()`**

Same pattern.

- [ ] **Step 5: Update example 4 (full pipeline) to use `build_graph()`**

This is the largest example (~170 lines). Move all node/edge definitions inside `build_graph(models=None)`. Move `HAS_CODEX`/`HAS_GEMINI` inside the function (derived from the `models` parameter). The `_wait_for_ci_fn` and `_decision_fn` helper functions can stay module-level since they don't affect graph topology.

- [ ] **Step 6: Smoke test the updated skill**

Generate a simple workflow using the updated skill to verify it produces a `build_graph()` function:

```bash
claude -p "Use /workflow to generate a workflow that runs pytest and commits if tests pass. Generate only, don't execute." \
    --model sonnet \
    --permission-mode bypassPermissions \
    --plugin-dir dev-loop
```

Check the generated `/tmp/workflow_*.py` file has `def build_graph(` in it.

- [ ] **Step 7: Add a note about `build_graph()` requirement**

In the "Important rules" section at the bottom, add:

```
- **Use `build_graph()`.** Always put graph construction in a `build_graph(models=None)` function.
  The `if __name__` block calls it and runs the graph. This makes scripts importable for testing.
  `build_graph()` must accept an optional `models` dict (defaulting to `detect_available_models()`)
  so callers can control model availability.
```

- [ ] **Step 8: Commit**

```bash
git add dev-loop/commands/workflow.md
git commit -m "feat(workflow): require build_graph() in generated scripts

Update script template and examples to put graph construction in a
build_graph(models=None) function. Makes generated scripts importable
for structural inspection by the eval framework."
```

---

### Task 3: Create GraphInspector helper

**Files:**
- Create: `dev-loop/tests/workflow_eval/__init__.py`
- Create: `dev-loop/tests/workflow_eval/helpers.py`
- Create: `dev-loop/tests/workflow_eval/test_helpers.py`

- [ ] **Step 1: Create the directory and `__init__.py`**

```bash
mkdir -p dev-loop/tests/workflow_eval/scenarios
touch dev-loop/tests/workflow_eval/__init__.py
```

- [ ] **Step 2: Write tests for GraphInspector**

Create `dev-loop/tests/workflow_eval/test_helpers.py`:

```python
"""Unit tests for GraphInspector."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure engine and helpers are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from engine import END, StateGraph, claude_node, shell_node, python_node, template_node

from helpers import GraphInspector


def _make_bugfix_graph() -> StateGraph:
    """Build a typical bug-fix workflow graph for testing."""
    graph = StateGraph(max_iterations=5)

    graph.add_node("run_tests", shell_node("pytest -x", output_key="test_output", check=False))
    graph.add_node("fix", claude_node("Fix: {test_output}", output_key="fix_output", model="sonnet", effort="medium"))
    graph.add_node("commit", shell_node("git commit -am 'fix'", output_key="commit_output"))

    def test_router(state: dict[str, str]) -> str:
        return "pass" if "passed" in state.get("test_output", "").lower() else "fail"

    graph.add_edge("start", "run_tests")
    graph.add_conditional_edges("run_tests", test_router, {"fail": "fix", "pass": "commit"})
    graph.add_edge("fix", "run_tests")
    graph.add_edge("commit", END)
    return graph


def _make_parallel_review_graph() -> StateGraph:
    """Build a graph with parallel review nodes."""
    graph = StateGraph(max_iterations=3)

    graph.add_node("implement", claude_node("Implement {spec}", model="sonnet", effort="medium"))
    graph.add_node("fan_out", python_node(lambda s: {}))
    graph.add_node("code_review", claude_node("Review code", model="opus", effort="high"))
    graph.add_node("security_review", claude_node("Security review", model="opus", effort="high"))
    graph.add_node("commit", shell_node("git commit -am 'done'"))

    graph.add_edge("start", "implement")
    graph.add_edge("implement", "fan_out")
    graph.add_parallel_edges("fan_out", ["code_review", "security_review"])
    graph.add_edge("code_review", "commit")
    graph.add_edge("security_review", "commit")
    graph.add_edge("commit", END)
    return graph


class TestHasNodeWithMeta:
    def test_finds_shell_node(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_node_with_meta("shell")

    def test_finds_claude_node(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_node_with_meta("claude")

    def test_finds_either(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_node_with_meta("codex", "claude")

    def test_no_match(self):
        h = GraphInspector(_make_bugfix_graph())
        assert not h.has_node_with_meta("gemini")


class TestGetNodesWithMeta:
    def test_returns_matching_nodes(self):
        h = GraphInspector(_make_bugfix_graph())
        nodes = h.get_nodes_with_meta("shell")
        assert len(nodes) == 2  # run_tests and commit
        assert "run_tests" in nodes
        assert "commit" in nodes

    def test_returns_empty_for_no_match(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.get_nodes_with_meta("gemini") == []


class TestHasNodeWithPromptContaining:
    def test_finds_keyword_case_insensitive(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_node_with_prompt_containing("pytest")

    def test_no_match(self):
        h = GraphInspector(_make_bugfix_graph())
        assert not h.has_node_with_prompt_containing("playwright")


class TestGetNodesWithPromptContaining:
    def test_returns_matching_nodes(self):
        h = GraphInspector(_make_bugfix_graph())
        nodes = h.get_nodes_with_prompt_containing("fix")
        assert "fix" in nodes  # claude_node prompt contains "Fix:"


class TestHasParallelGroup:
    def test_true_when_parallel(self):
        h = GraphInspector(_make_parallel_review_graph())
        assert h.has_parallel_group()

    def test_false_when_no_parallel(self):
        h = GraphInspector(_make_bugfix_graph())
        assert not h.has_parallel_group()


class TestHasParallelGroupContaining:
    def test_finds_matching_meta(self):
        h = GraphInspector(_make_parallel_review_graph())
        assert h.has_parallel_group_containing("claude")

    def test_no_match(self):
        h = GraphInspector(_make_parallel_review_graph())
        assert not h.has_parallel_group_containing("gemini")


class TestHasConditionalLoop:
    def test_finds_loop(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_conditional_loop()

    def test_finds_loop_with_meta_filter(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_conditional_loop(meta_contains="shell")

    def test_no_loop_with_wrong_meta(self):
        h = GraphInspector(_make_bugfix_graph())
        assert not h.has_conditional_loop(meta_contains="claude")

    def test_no_loop_in_linear_graph(self):
        h = GraphInspector(_make_parallel_review_graph())
        assert not h.has_conditional_loop()


class TestNodeCount:
    def test_bugfix_graph(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.node_count() == 3

    def test_parallel_graph(self):
        h = GraphInspector(_make_parallel_review_graph())
        assert h.node_count() == 5


class TestGetEdgeTargets:
    def test_unconditional(self):
        h = GraphInspector(_make_bugfix_graph())
        targets = h.get_edge_targets("fix")
        assert "run_tests" in targets

    def test_conditional(self):
        h = GraphInspector(_make_bugfix_graph())
        targets = h.get_edge_targets("run_tests")
        assert "fix" in targets
        assert "commit" in targets


class TestHasEdgeToEnd:
    def test_true(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.has_edge_to_end()

    def test_false_when_no_end():
        graph = StateGraph()
        graph.add_node("a", python_node(lambda s: {}))
        graph.add_node("b", python_node(lambda s: {}))
        graph.add_edge("start", "a")
        graph.add_edge("a", "b")
        h = GraphInspector(graph)
        assert not h.has_edge_to_end()


class TestGetModelForNode:
    def test_finds_model(self):
        h = GraphInspector(_make_bugfix_graph())
        model = h.get_model_for_node("fix")
        assert model is not None
        assert "sonnet" in model

    def test_no_match(self):
        h = GraphInspector(_make_bugfix_graph())
        assert h.get_model_for_node("nonexistent") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest dev-loop/tests/workflow_eval/test_helpers.py -v`
Expected: ImportError (helpers module doesn't exist)

- [ ] **Step 4: Implement GraphInspector**

Create `dev-loop/tests/workflow_eval/helpers.py`:

```python
"""GraphInspector — pattern-matching query interface for StateGraph inspection.

All meta methods use substring matching against _diagram_label values in _node_meta.
All prompt methods use case-insensitive substring matching against _prompt_template.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from engine import StateGraph, _EndSentinel


class GraphInspector:
    """Pattern-matching query interface for StateGraph inspection."""

    def __init__(self, graph: StateGraph) -> None:
        self.graph = graph

    def has_node_with_meta(self, *patterns: str) -> bool:
        """True if any node's _node_meta value contains one of the patterns."""
        for meta in self.graph._node_meta.values():
            if any(p in meta for p in patterns):
                return True
        return False

    def get_nodes_with_meta(self, pattern: str) -> list[str]:
        """Return node names whose _node_meta value contains pattern."""
        return [name for name, meta in self.graph._node_meta.items() if pattern in meta]

    def has_node_with_prompt_containing(self, keyword: str) -> bool:
        """True if any node's _prompt_template contains keyword (case-insensitive)."""
        kw = keyword.lower()
        for fn in self.graph._nodes.values():
            tpl = getattr(fn, "_prompt_template", None)
            if tpl and kw in tpl.lower():
                return True
        return False

    def get_nodes_with_prompt_containing(self, keyword: str) -> list[str]:
        """Return node names whose _prompt_template contains keyword."""
        kw = keyword.lower()
        result = []
        for name, fn in self.graph._nodes.items():
            tpl = getattr(fn, "_prompt_template", None)
            if tpl and kw in tpl.lower():
                result.append(name)
        return result

    def has_parallel_group(self) -> bool:
        """True if the graph uses add_parallel_edges."""
        return len(self.graph._parallel_edges) > 0

    def has_parallel_group_containing(self, *meta_patterns: str) -> bool:
        """True if any parallel group's targets include nodes matching the patterns."""
        for targets in self.graph._parallel_edges.values():
            for t in targets:
                meta = self.graph._node_meta.get(t, "")
                if any(p in meta for p in meta_patterns):
                    return True
        return False

    def has_conditional_loop(self, meta_contains: str | None = None) -> bool:
        """True if a conditional edge creates a back-edge (cycle).

        For each conditional edge, check if any route_map target is a node
        that has an edge (unconditional or parallel) leading toward the
        conditional edge's source — i.e., the target is an ancestor.

        Simplified: check if any route_map target also appears as a source
        of an unconditional edge whose target chain reaches the conditional
        edge's source node.
        """
        # Build set of all nodes that are sources of unconditional/parallel edges
        # leading to each node (reverse adjacency)
        ancestors: dict[str, set[str]] = {}
        for name in self.graph._nodes:
            ancestors[name] = self._find_ancestors(name)

        for ce in self.graph._conditional_edges:
            if meta_contains:
                source_meta = self.graph._node_meta.get(ce.source, "")
                if meta_contains not in source_meta:
                    continue
            for target in ce.route_map.values():
                if isinstance(target, _EndSentinel):
                    continue
                # Back-edge: target is an ancestor of the source
                if target in ancestors.get(ce.source, set()):
                    return True
                # Direct back-edge: target IS the source
                if target == ce.source:
                    return True
        return False

    def _find_ancestors(self, node: str) -> set[str]:
        """Find all ancestor nodes (nodes from which `node` is reachable)."""
        ancestors: set[str] = set()
        visited: set[str] = set()
        queue = [node]
        while queue:
            current = queue.pop()
            if current in visited:
                continue
            visited.add(current)
            # Find nodes that have an edge TO current
            for edge in self.graph._edges:
                if edge.target == current and edge.source not in ancestors:
                    ancestors.add(edge.source)
                    queue.append(edge.source)
            for source, targets in self.graph._parallel_edges.items():
                if current in targets and source not in ancestors:
                    ancestors.add(source)
                    queue.append(source)
        return ancestors

    def node_count(self) -> int:
        """Total number of nodes in the graph."""
        return len(self.graph._nodes)

    def get_edge_targets(self, source_pattern: str) -> list[str]:
        """All non-END targets reachable from nodes whose name contains source_pattern.

        Excludes END sentinels — use has_edge_to_end() to check termination.
        """
        targets: list[str] = []
        for edge in self.graph._edges:
            if source_pattern in edge.source and not isinstance(edge.target, _EndSentinel):
                targets.append(edge.target)
        for ce in self.graph._conditional_edges:
            if source_pattern in ce.source:
                for t in ce.route_map.values():
                    if not isinstance(t, _EndSentinel):
                        targets.append(t)
        for source, parallel_targets in self.graph._parallel_edges.items():
            if source_pattern in source:
                targets.extend(parallel_targets)
        return targets

    def has_edge_to_end(self) -> bool:
        """True if the graph has at least one edge to END."""
        for edge in self.graph._edges:
            if isinstance(edge.target, _EndSentinel):
                return True
        for ce in self.graph._conditional_edges:
            for t in ce.route_map.values():
                if isinstance(t, _EndSentinel):
                    return True
        return False

    def get_model_for_node(self, node_name_pattern: str) -> str | None:
        """Return the _node_meta for the first node whose name contains pattern."""
        for name, meta in self.graph._node_meta.items():
            if node_name_pattern in name:
                return meta
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest dev-loop/tests/workflow_eval/test_helpers.py -v`
Expected: All tests pass

- [ ] **Step 6: Run linting and type checking**

Run: `uv run ruff check dev-loop/tests/workflow_eval/ && uv run pyright`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add dev-loop/tests/workflow_eval/
git commit -m "feat(eval): add GraphInspector for structural workflow inspection

Pattern-matching query interface over StateGraph internals. Supports
substring matching on node meta (model/effort), prompt templates,
parallel groups, conditional loops (cycle detection), and edge topology."
```

---

### Task 4: Create test runner

**Files:**
- Create: `dev-loop/tests/workflow_eval/test_scenarios.py`
- Modify: `pyproject.toml` (add `eval` marker)

- [ ] **Step 1: Register the `eval` pytest marker**

Add to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "eval: LLM-based eval tests (slow, requires Claude CLI)",
]
```

- [ ] **Step 2: Write the test runner**

Create `dev-loop/tests/workflow_eval/test_scenarios.py`:

```python
"""Workflow generation eval runner.

Discovers scenarios under scenarios/, generates workflow scripts via
headless Claude with the dev-loop plugin, imports the graph, and runs
scenario-specific assertions.

Usage:
    uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -v
    uv run pytest -m eval -v
    uv run pytest -m eval -k bugfix_bare -v -s
"""

from __future__ import annotations

import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

# Ensure engine and helpers are importable
PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import StateGraph

from helpers import GraphInspector

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def discover_scenarios() -> list[Path]:
    """Find all scenarios/*/scenario.toml, return parent dirs."""
    return sorted(
        p.parent for p in SCENARIOS_DIR.glob("*/scenario.toml")
    )


def scaffold_repo(scenario_dir: Path, tmp_path: Path) -> Path:
    """Copy scenario's repo/ tree to tmp_path, run git init."""
    repo_src = scenario_dir / "repo"
    repo_dst = tmp_path / "repo"
    if repo_src.exists():
        shutil.copytree(repo_src, repo_dst)
    else:
        repo_dst.mkdir()

    # Init git so workflow scripts can use git commands
    subprocess.run(["git", "init"], cwd=repo_dst, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dst, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "--allow-empty"],
        cwd=repo_dst,
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    return repo_dst


def _cleanup_stale_workflows() -> None:
    """Remove stale workflow files from /tmp to avoid glob collisions."""
    for f in glob.glob("/tmp/workflow_*.py"):
        try:
            # Only remove files older than 60 seconds
            if time.time() - os.path.getmtime(f) > 60:
                os.unlink(f)
        except OSError:
            pass


def generate_workflow(prompt: str, repo_path: Path) -> Path:
    """Generate a workflow script via headless Claude with the dev-loop plugin.

    Returns path to the generated workflow.py file.
    """
    _cleanup_stale_workflows()

    # Record existing workflow files to find the new one
    before = set(glob.glob("/tmp/workflow_*.py"))

    combined_prompt = (
        f"Use /workflow to generate a workflow for the following task. "
        f"Generate the script only, do not execute it.\n\n"
        f"Task: {prompt}"
    )

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    result = subprocess.run(
        [
            "claude", "-p", combined_prompt,
            "--model", "sonnet",
            "--permission-mode", "bypassPermissions",
            "--plugin-dir", str(PLUGIN_DIR),
            "--output-format", "json",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,  # 5 minute timeout
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Claude failed to generate workflow (exit {result.returncode}):\n"
            f"stderr: {result.stderr[:1000]}"
        )

    # Find the newly created workflow file
    after = set(glob.glob("/tmp/workflow_*.py"))
    new_files = after - before
    if not new_files:
        raise RuntimeError(
            f"No workflow file generated in /tmp/. Claude output:\n{result.stdout[:2000]}"
        )

    # Return the most recently modified new file
    return Path(max(new_files, key=os.path.getmtime))


def import_graph(workflow_path: Path) -> StateGraph:
    """Import build_graph() from the generated script and return the graph.

    Sets up sys.path and env so the script's imports resolve.
    """
    # Set CLAUDE_PLUGIN_ROOT so the script's sys.path.insert resolves
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)

    # Ensure engine is importable from the script's perspective
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    # Safety check: verify the script has build_graph() and if __name__ guard
    # before importing. If not, the import would execute the workflow.
    script_text = workflow_path.read_text()
    if "def build_graph" not in script_text:
        raise RuntimeError(
            f"Generated script {workflow_path} does not contain a build_graph() function. "
            f"Cannot import safely. First 500 chars:\n{script_text[:500]}"
        )
    if 'if __name__' not in script_text:
        raise RuntimeError(
            f"Generated script {workflow_path} has no if __name__ guard. "
            f"Importing would execute the workflow."
        )

    spec = importlib.util.spec_from_file_location("workflow_gen", workflow_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {workflow_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    build_graph = getattr(module, "build_graph", None)
    if build_graph is None:
        raise RuntimeError(
            f"Generated script {workflow_path} does not have a build_graph() function"
        )

    # Pass all models as available for deterministic topology
    return build_graph(models={"claude": True, "codex": True, "gemini": True})


scenarios = discover_scenarios()


@pytest.mark.eval
@pytest.mark.parametrize(
    "scenario_dir",
    scenarios,
    ids=lambda p: p.name,
)
def test_workflow_scenario(scenario_dir: Path, tmp_path: Path) -> None:
    """Generate a workflow for the scenario and run structural assertions."""
    with open(scenario_dir / "scenario.toml", "rb") as f:
        config = tomllib.load(f)

    repo_path = scaffold_repo(scenario_dir, tmp_path)
    workflow_path = generate_workflow(config["prompt"], repo_path)

    # Save a copy for inspection
    results_dir = SCENARIOS_DIR.parent / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    saved = results_dir / f"{scenario_dir.name}_{timestamp}.py"
    shutil.copy2(workflow_path, saved)

    graph = import_graph(workflow_path)
    inspector = GraphInspector(graph)

    # Load and run scenario-specific assertions
    assertions_path = scenario_dir / "assertions.py"
    if not assertions_path.exists():
        pytest.fail(f"No assertions.py in {scenario_dir}")

    spec = importlib.util.spec_from_file_location("assertions", assertions_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"Cannot load assertions from {assertions_path}")
    assertions_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assertions_module)

    assert_fn = getattr(assertions_module, "assert_workflow", None)
    if assert_fn is None:
        pytest.fail(f"assertions.py in {scenario_dir} missing assert_workflow()")

    assert_fn(graph, inspector)
```

- [ ] **Step 3: Run linting and type checking**

Run: `uv run ruff check dev-loop/tests/workflow_eval/test_scenarios.py && uv run pyright`
Expected: Clean

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/test_scenarios.py pyproject.toml
git commit -m "feat(eval): add workflow eval test runner

Pytest-based runner that discovers scenarios, generates workflows via
headless Claude with --plugin-dir, imports the graph, and runs
scenario-specific assertions. Marked with @pytest.mark.eval."
```

---

### Task 5: Create scenario — `bugfix_bare`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_bare/scenario.toml`
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_bare/repo/src/mathlib.py`
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_bare/repo/tests/test_mathlib.py`
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_bare/repo/pyproject.toml`
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_bare/assertions.py`

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Bug fix on bare repo"
prompt = """
Fix the calculate_average function in src/mathlib.py —
it crashes on empty lists with a ZeroDivisionError.
The test in tests/test_mathlib.py already covers this case.
"""
```

- [ ] **Step 2: Create repo scaffold**

`repo/src/mathlib.py`:
```python
def calculate_average(numbers: list[float]) -> float:
    """Return the average of a list of numbers."""
    return sum(numbers) / len(numbers)
```

`repo/tests/test_mathlib.py`:
```python
from src.mathlib import calculate_average

def test_average_normal():
    assert calculate_average([1, 2, 3]) == 2.0

def test_average_empty():
    assert calculate_average([]) == 0.0
```

`repo/pyproject.toml`:
```toml
[project]
name = "mathlib"
version = "0.1.0"
requires-python = ">=3.12"
```

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Bug fix on bare repo: minimal test→fix loop."""

    # Must have a shell node for running tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Must have an LLM node for fixing
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"

    # Test failure should loop back to fix (conditional back-edge)
    assert h.has_conditional_loop(), "test→fix loop expected"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # Bare repo — no doc update, lint, or typecheck steps expected
    assert not h.has_node_with_prompt_containing("documentation"), \
        "no doc step expected on bare repo"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/bugfix_bare/
git commit -m "feat(eval): add bugfix_bare scenario

Minimal bug-fix scenario on a bare repo. Asserts test→fix loop,
shell node for tests, LLM node for fix, no unnecessary steps."
```

---

### Task 6: Create scenario — `bugfix_backend_instructions`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_backend_instructions/scenario.toml`
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_backend_instructions/repo/` (src, tests, CLAUDE.md, pyproject.toml)
- Create: `dev-loop/tests/workflow_eval/scenarios/bugfix_backend_instructions/assertions.py`

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Bug fix on backend repo with dev instructions"
prompt = """
Fix the calculate_average function in src/mathlib.py —
it crashes on empty lists with a ZeroDivisionError.
The test in tests/test_mathlib.py already covers this case.
"""
```

- [ ] **Step 2: Create repo scaffold (same mathlib as bugfix_bare, plus CLAUDE.md)**

`repo/CLAUDE.md`:
```markdown
# Development Guidelines

## Quality Gates
- Always run `uv run pytest` after any code change
- Always run `uv run ruff check .` for linting
- Always run `uv run pyright` for type checking
- Update documentation in docs/ when changing public APIs
```

Same `src/mathlib.py`, `tests/test_mathlib.py`, `pyproject.toml` as bugfix_bare.

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Bug fix on backend repo with CLAUDE.md instructions."""

    # Same core assertions as bugfix_bare
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"
    assert h.has_conditional_loop(), "test→fix loop expected"
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # CLAUDE.md says to run lint and typecheck — should pick those up
    assert h.has_node_with_prompt_containing("ruff") or h.has_node_with_prompt_containing("lint"), \
        "should include linting (from CLAUDE.md instructions)"
    assert h.has_node_with_prompt_containing("pyright") or h.has_node_with_prompt_containing("type"), \
        "should include type checking (from CLAUDE.md instructions)"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/bugfix_backend_instructions/
git commit -m "feat(eval): add bugfix_backend_instructions scenario

Bug-fix scenario with CLAUDE.md specifying lint/typecheck/doc gates.
Asserts those quality gates appear in the generated workflow."
```

---

### Task 7: Create scenario — `feature_bare`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/feature_bare/` (scenario.toml, repo/, assertions.py)

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Feature development on bare repo"
prompt = """
Add a new function calculate_median(numbers: list[float]) -> float
to src/mathlib.py. It should return the median value of a list of numbers.
Also add tests for it in tests/test_mathlib.py.
"""
```

- [ ] **Step 2: Create repo scaffold**

Same base as bugfix_bare (mathlib.py with calculate_average, test file, pyproject.toml) but the function works correctly — no bug.

`repo/src/mathlib.py`:
```python
def calculate_average(numbers: list[float]) -> float:
    """Return the average of a list of numbers."""
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)
```

`repo/tests/test_mathlib.py`:
```python
from src.mathlib import calculate_average

def test_average_normal():
    assert calculate_average([1, 2, 3]) == 2.0

def test_average_empty():
    assert calculate_average([]) == 0.0
```

Same `pyproject.toml`.

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Feature development on bare repo."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Must have a shell node to run tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Should have a commit step
    assert h.has_node_with_prompt_containing("commit") or h.has_node_with_prompt_containing("git"), \
        "should commit the changes"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/feature_bare/
git commit -m "feat(eval): add feature_bare scenario

Feature development on bare repo. Asserts implementation node,
test runner, commit step, and proper termination."
```

---

### Task 8: Create scenario — `feature_backend_instructions`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/feature_backend_instructions/` (scenario.toml, repo/, assertions.py)

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Feature development on backend repo with instructions"
prompt = """
Add a new function calculate_median(numbers: list[float]) -> float
to src/mathlib.py. It should return the median value of a list of numbers.
Also add tests for it in tests/test_mathlib.py.
"""
```

- [ ] **Step 2: Create repo scaffold**

Same as feature_bare, plus `CLAUDE.md`:

```markdown
# Development Guidelines

## Quality Gates
- Always run `uv run pytest` after any code change
- Always run `uv run ruff check .` for linting
- Always run `uv run pyright` for type checking
- Use pydantic models for any data validation
- Update documentation in docs/ when adding new public functions
```

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Feature dev on backend repo with quality gate instructions."""

    # Core: implement + test + commit
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # CLAUDE.md quality gates should be picked up
    assert h.has_node_with_prompt_containing("ruff") or h.has_node_with_prompt_containing("lint"), \
        "should include linting (from CLAUDE.md)"
    assert h.has_node_with_prompt_containing("pyright") or h.has_node_with_prompt_containing("type"), \
        "should include type checking (from CLAUDE.md)"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/feature_backend_instructions/
git commit -m "feat(eval): add feature_backend_instructions scenario

Feature dev with CLAUDE.md quality gates. Asserts lint/typecheck
steps are picked up from repo instructions."
```

---

### Task 9: Create scenario — `frontend_feature_instructions`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/frontend_feature_instructions/` (scenario.toml, repo/, assertions.py)

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Frontend feature with Playwright instructions"
prompt = """
Add a new Settings page component at src/components/Settings.tsx.
It should display user preferences and allow toggling dark mode.
Add e2e tests for it.
"""
```

- [ ] **Step 2: Create repo scaffold**

`repo/package.json`:
```json
{
  "name": "my-app",
  "version": "1.0.0",
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "test": "npx playwright test"
  },
  "dependencies": {
    "next": "^14.0.0",
    "react": "^18.0.0"
  },
  "devDependencies": {
    "@playwright/test": "^1.40.0"
  }
}
```

`repo/playwright.config.ts`:
```typescript
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e',
  use: { baseURL: 'http://localhost:3000' },
});
```

`repo/src/components/Header.tsx`:
```typescript
export default function Header() {
  return <header><h1>My App</h1></header>;
}
```

`repo/tests/e2e/header.spec.ts`:
```typescript
import { test, expect } from '@playwright/test';
test('header visible', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toBeVisible();
});
```

`repo/CLAUDE.md`:
```markdown
# Development Guidelines

## Testing
- Run e2e tests with `npx playwright test` after any UI change
- Use Playwright for all end-to-end tests
- Component tests go in tests/e2e/

## Quality Gates
- Run `npm run build` to verify no build errors
- Always test on both light and dark mode
```

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Frontend feature with Playwright instructions."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Must reference Playwright in some node's prompt (from CLAUDE.md)
    assert h.has_node_with_prompt_containing("playwright"), \
        "should include playwright testing (from CLAUDE.md instructions)"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/frontend_feature_instructions/
git commit -m "feat(eval): add frontend_feature_instructions scenario

Frontend feature with Playwright e2e testing instructions. Asserts
workflow picks up playwright testing from CLAUDE.md."
```

---

### Task 10: Create scenario — `fullstack_feature_instructions`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/fullstack_feature_instructions/` (scenario.toml, repo/, assertions.py)

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Full-stack feature with comprehensive instructions"
prompt = """
Add a user profile feature:
- Backend: Add a /api/profile endpoint in src/api/routes.py that returns user profile data
- Frontend: Add a Profile page component at src/components/Profile.tsx that displays the data
- Add both backend tests (pytest) and frontend e2e tests (playwright)
"""
```

- [ ] **Step 2: Create repo scaffold**

`repo/src/api/routes.py`:
```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/health")
def health():
    return {"status": "ok"}
```

`repo/src/components/Header.tsx`:
```typescript
export default function Header() {
  return <header><h1>My App</h1></header>;
}
```

`repo/tests/test_api.py`:
```python
def test_health():
    from src.api.routes import app
    # basic smoke test
    assert app is not None
```

`repo/tests/e2e/header.spec.ts`:
```typescript
import { test, expect } from '@playwright/test';
test('header visible', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('h1')).toBeVisible();
});
```

`repo/package.json`:
```json
{
  "name": "fullstack-app",
  "version": "1.0.0",
  "scripts": {
    "dev": "next dev",
    "test": "npx playwright test"
  },
  "dependencies": { "next": "^14.0.0", "react": "^18.0.0" },
  "devDependencies": { "@playwright/test": "^1.40.0" }
}
```

`repo/playwright.config.ts`:
```typescript
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e',
  use: { baseURL: 'http://localhost:3000' },
});
```

`repo/pyproject.toml`:
```toml
[project]
name = "fullstack-app"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi"]
```

`repo/CLAUDE.md`:
```markdown
# Development Guidelines

## Testing
- Backend: Run `uv run pytest` after any Python change
- Frontend: Run `npx playwright test` after any UI change
- Both test suites must pass before committing

## Quality Gates
- Run `uv run ruff check .` for Python linting
- Run `uv run pyright` for Python type checking
- Run `npm run build` to verify frontend builds
- Always update documentation when adding new API endpoints or pages
```

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Full-stack feature with both pytest and playwright."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Should reference both test frameworks (from CLAUDE.md)
    assert h.has_node_with_prompt_containing("pytest") or h.has_node_with_prompt_containing("uv run pytest"), \
        "should include pytest for backend (from CLAUDE.md)"
    assert h.has_node_with_prompt_containing("playwright"), \
        "should include playwright for frontend (from CLAUDE.md)"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/fullstack_feature_instructions/
git commit -m "feat(eval): add fullstack_feature_instructions scenario

Full-stack feature with both pytest and playwright instructions.
Asserts both test frameworks appear in the generated workflow."
```

---

### Task 11: Create scenario — `refactoring_bare`

**Files:**
- Create: `dev-loop/tests/workflow_eval/scenarios/refactoring_bare/` (scenario.toml, repo/, assertions.py)

- [ ] **Step 1: Create scenario.toml**

```toml
name = "Refactoring on bare repo"
prompt = """
The src/mathlib.py file has grown too large. Extract the statistics-related
functions (calculate_average, calculate_std_dev) into a new module
src/stats.py. Update all imports accordingly.
"""
```

- [ ] **Step 2: Create repo scaffold**

`repo/src/mathlib.py`:
```python
import math

def calculate_average(numbers: list[float]) -> float:
    """Return the average of a list of numbers."""
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)

def calculate_std_dev(numbers: list[float]) -> float:
    """Return the standard deviation of a list of numbers."""
    if len(numbers) < 2:
        return 0.0
    avg = calculate_average(numbers)
    variance = sum((x - avg) ** 2 for x in numbers) / (len(numbers) - 1)
    return math.sqrt(variance)

def add(a: float, b: float) -> float:
    return a + b

def subtract(a: float, b: float) -> float:
    return a - b

def multiply(a: float, b: float) -> float:
    return a * b
```

`repo/tests/test_mathlib.py`:
```python
from src.mathlib import calculate_average, calculate_std_dev, add, subtract, multiply

def test_average():
    assert calculate_average([1, 2, 3]) == 2.0

def test_std_dev():
    result = calculate_std_dev([2, 4, 4, 4, 5, 5, 7, 9])
    assert abs(result - 2.0) < 0.1

def test_add():
    assert add(1, 2) == 3

def test_subtract():
    assert subtract(5, 3) == 2
```

`repo/pyproject.toml`:
```toml
[project]
name = "mathlib"
version = "0.1.0"
requires-python = ">=3.12"
```

- [ ] **Step 3: Create assertions.py**

```python
def assert_workflow(graph, h):
    """Refactoring on bare repo: extract module, run existing tests."""

    # Must have an LLM node for the refactoring
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to refactor"

    # Must run existing tests to verify refactoring didn't break anything
    assert h.has_node_with_meta("shell"), "needs a shell node to run existing tests"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
```

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/workflow_eval/scenarios/refactoring_bare/
git commit -m "feat(eval): add refactoring_bare scenario

Module extraction refactoring on bare repo. Asserts LLM node for
refactoring, shell node for running existing tests, proper termination."
```

---

### Task 12: Add results/ to .gitignore and update documentation

**Files:**
- Modify: `.gitignore` (or create if needed)
- Modify: `dev-loop/CLAUDE.md`

- [ ] **Step 1: Add results/ to .gitignore**

Add to `.gitignore`:
```
dev-loop/tests/workflow_eval/results/
```

- [ ] **Step 2: Update dev-loop/CLAUDE.md with eval testing instructions**

Add a new section:

```markdown
## Workflow eval tests

Eval tests verify that the `/workflow` skill generates structurally sound workflows. They call headless Claude to generate a workflow script, import the graph, and run Python assertions.

- Run all evals: `uv run pytest -m eval -v`
- Run a specific scenario: `uv run pytest -m eval -k bugfix_bare -v -s`
- Scenarios live in `tests/workflow_eval/scenarios/` — each is a self-contained directory
- Adding a scenario: create a new directory with `scenario.toml`, `repo/`, and `assertions.py`
- Generated scripts are saved to `tests/workflow_eval/results/` for manual inspection
- These tests make LLM calls — run them when iterating on the `/workflow` skill prompt, not on every commit
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore dev-loop/CLAUDE.md
git commit -m "docs: add workflow eval testing instructions and gitignore results"
```

---

### Task 13: Run a single eval scenario end-to-end

This is the validation step — verify the entire pipeline works.

- [ ] **Step 1: Run the bugfix_bare scenario**

Run: `uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -k bugfix_bare -v -s`
Expected: Claude generates a workflow, the graph is imported, assertions run. May pass or fail depending on LLM output — the important thing is that the pipeline works end-to-end (no crashes in scaffolding, generation, import, or assertion loading).

- [ ] **Step 2: If pipeline fails, debug and fix**

Common issues:
- Claude doesn't write to `/tmp/workflow_*.py` → check the prompt phrasing
- `build_graph()` not found → Claude didn't follow the updated template → check workflow.md
- Import errors → check sys.path setup in `import_graph`
- Assertion failures → expected on first run, these indicate what the LLM actually generates

- [ ] **Step 3: Inspect the saved result**

Check `dev-loop/tests/workflow_eval/results/` for the saved workflow script. Manually review:
- Does it have a `build_graph()` function?
- Does the graph topology make sense for a bug fix?
- Are model choices reasonable?

- [ ] **Step 4: Run all scenarios**

Run: `uv run pytest -m eval -v -s`
Expected: All 7 scenarios run through the pipeline. Some assertions may fail — that's the eval working. The pipeline itself should not crash.
