"""Unit tests for GraphInspector."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure engine and helpers are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import END, StateGraph, claude_node, python_node, shell_node
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

    def test_false_when_no_end(self):
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
