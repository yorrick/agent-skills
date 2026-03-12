#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest", "pytest-asyncio"]
# ///
"""Unit tests for the workflow engine."""

from __future__ import annotations

# Ensure engine module is importable
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from engine import END, StateGraph


def test_add_node():
    """Can add nodes to a graph."""
    graph = StateGraph()

    async def my_node(state: dict[str, str]) -> dict[str, str]:
        return {"out": "hello"}

    graph.add_node("test", my_node)
    assert "test" in graph._nodes


def test_add_edge():
    """Can add edges between nodes."""
    graph = StateGraph()

    async def noop(state: dict[str, str]) -> dict[str, str]:
        return {}

    graph.add_node("a", noop)
    graph.add_node("b", noop)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    assert len(graph._edges) == 2


def test_add_conditional_edges():
    """Can add conditional edges with a router."""
    graph = StateGraph()

    async def noop(state: dict[str, str]) -> dict[str, str]:
        return {}

    graph.add_node("a", noop)
    graph.add_node("b", noop)

    def router(state: dict[str, str]) -> str:
        return "b"

    graph.add_conditional_edges("a", router, {"b": "b", "end": END})
    assert len(graph._conditional_edges) == 1


def test_add_parallel_edges():
    """Can add parallel edges from one source to multiple targets."""
    graph = StateGraph()

    async def noop(state: dict[str, str]) -> dict[str, str]:
        return {}

    graph.add_node("source", noop)
    graph.add_node("target1", noop)
    graph.add_node("target2", noop)
    graph.add_parallel_edges("source", ["target1", "target2"])
    assert "source" in graph._parallel_edges


def test_end_sentinel():
    """END is a unique sentinel."""
    assert END != "end"
    assert str(END) == "END"
