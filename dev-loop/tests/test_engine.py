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

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from engine import (
    END,
    MaxIterationsExceeded,
    State,
    StateGraph,
    claude_node,
    codex_node,
    detect_available_models,
    gemini_node,
    python_node,
    shell_node,
    template_node,
)


async def _noop(state: dict[str, str]) -> dict[str, str]:
    return {}


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
    graph.add_node("a", _noop)
    graph.add_node("b", _noop)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    assert len(graph._edges) == 2


def test_add_conditional_edges():
    """Can add conditional edges with a router."""
    graph = StateGraph()
    graph.add_node("a", _noop)
    graph.add_node("b", _noop)

    def router(state: dict[str, str]) -> str:
        return "b"

    graph.add_conditional_edges("a", router, {"b": "b", "end": END})
    assert len(graph._conditional_edges) == 1


def test_add_parallel_edges():
    """Can add parallel edges from one source to multiple targets."""
    graph = StateGraph()
    graph.add_node("source", _noop)
    graph.add_node("target1", _noop)
    graph.add_node("target2", _noop)
    graph.add_parallel_edges("source", ["target1", "target2"])
    assert "source" in graph._parallel_edges


def test_end_sentinel():
    """END is a unique sentinel."""
    assert END != "end"
    assert str(END) == "END"


# --- Task 2: Linear and conditional execution ---


@pytest.mark.asyncio
async def test_linear_execution():
    """Nodes execute in edge order, state accumulates."""
    graph = StateGraph()

    async def node_a(state: State) -> State:
        return {"a_out": "from_a"}

    async def node_b(state: State) -> State:
        return {"b_out": f"from_b_got_{state['a_out']}"}

    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)

    result = await graph.run({"input": "hello"})
    assert result["a_out"] == "from_a"
    assert result["b_out"] == "from_b_got_from_a"
    assert result["input"] == "hello"  # initial state preserved


@pytest.mark.asyncio
async def test_conditional_execution():
    """Conditional edges route based on router function."""
    graph = StateGraph()

    async def check(state: State) -> State:
        return {"checked": "true"}

    async def fix(state: State) -> State:
        return {"fixed": "true"}

    async def done(state: State) -> State:
        return {"done": "true"}

    def router(state: State) -> str:
        if state.get("fixed"):
            return "done"
        return "fix"

    graph.add_node("check", check)
    graph.add_node("fix", fix)
    graph.add_node("done", done)

    graph.add_edge("start", "check")
    graph.add_conditional_edges("check", router, {"fix": "fix", "done": "done"})
    graph.add_edge("fix", "check")  # loop back
    graph.add_edge("done", END)

    result = await graph.run({})
    assert result["fixed"] == "true"
    assert result["done"] == "true"


@pytest.mark.asyncio
async def test_max_iterations_exceeded():
    """Engine raises MaxIterationsExceeded when loop limit is hit."""
    graph = StateGraph(max_iterations=2)

    counter = {"n": 0}

    async def increment(state: State) -> State:
        counter["n"] += 1
        return {"count": str(counter["n"])}

    def always_loop(state: State) -> str:
        return "loop"

    graph.add_node("step", increment)
    graph.add_edge("start", "step")
    graph.add_conditional_edges("step", always_loop, {"loop": "step", "done": END})

    with pytest.raises(MaxIterationsExceeded):
        await graph.run({})


@pytest.mark.asyncio
async def test_max_iterations_multi_node_cycle():
    """max_iterations counts full cycles, not per-node re-visits.

    A cycle through N nodes should count as 1 iteration, not N.
    With max_iterations=2, a 3-node cycle should complete 2 full loops.
    """
    graph = StateGraph(max_iterations=2)

    counter = {"n": 0}

    async def step_a(state: State) -> State:
        counter["n"] += 1
        return {"a_count": str(counter["n"])}

    async def step_b(state: State) -> State:
        return {}

    async def step_c(state: State) -> State:
        return {}

    def always_loop(state: State) -> str:
        return "loop"

    graph.add_node("a", step_a)
    graph.add_node("b", step_b)
    graph.add_node("c", step_c)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_conditional_edges("c", always_loop, {"loop": "a", "done": END})

    with pytest.raises(MaxIterationsExceeded):
        await graph.run({})

    # step_a should have run max_iterations + 1 = 3 times (first visit + 2 iterations)
    assert counter["n"] == 3


# --- Task 3: Parallel execution ---


@pytest.mark.asyncio
async def test_parallel_execution():
    """Parallel targets run concurrently and join node waits for all."""
    graph = StateGraph()
    execution_order: list[str] = []

    async def source(state: State) -> State:
        execution_order.append("source")
        return {"source_out": "done"}

    async def parallel_a(state: State) -> State:
        execution_order.append("a_start")
        await asyncio.sleep(0.05)
        execution_order.append("a_end")
        return {"a_out": "from_a"}

    async def parallel_b(state: State) -> State:
        execution_order.append("b_start")
        await asyncio.sleep(0.01)
        execution_order.append("b_end")
        return {"b_out": "from_b"}

    async def join(state: State) -> State:
        execution_order.append("join")
        return {"joined": f"{state['a_out']}+{state['b_out']}"}

    graph.add_node("source", source)
    graph.add_node("parallel_a", parallel_a)
    graph.add_node("parallel_b", parallel_b)
    graph.add_node("join", join)

    graph.add_edge("start", "source")
    graph.add_parallel_edges("source", ["parallel_a", "parallel_b"])
    graph.add_edge("parallel_a", "join")
    graph.add_edge("parallel_b", "join")
    graph.add_edge("join", END)

    result = await graph.run({})

    assert result["a_out"] == "from_a"
    assert result["b_out"] == "from_b"
    assert result["joined"] == "from_a+from_b"
    # Both started before either ended (concurrent)
    assert "a_start" in execution_order
    assert "b_start" in execution_order
    assert execution_order.index("join") > execution_order.index("a_end")
    assert execution_order.index("join") > execution_order.index("b_end")


@pytest.mark.asyncio
async def test_parallel_then_conditional():
    """Parallel execution followed by conditional edge works."""
    graph = StateGraph()

    async def source(state: State) -> State:
        return {}

    async def review_a(state: State) -> State:
        return {"review_a": "issues found"}

    async def review_b(state: State) -> State:
        return {"review_b": "clean"}

    async def decision(state: State) -> State:
        has_issues = "issues" in state.get("review_a", "") or "issues" in state.get("review_b", "")
        return {"needs_fix": "yes" if has_issues else "no"}

    async def fix(state: State) -> State:
        return {"fixed": "true"}

    def router(state: State) -> str:
        return "fix" if state.get("needs_fix") == "yes" else "done"

    graph.add_node("source", source)
    graph.add_node("review_a", review_a)
    graph.add_node("review_b", review_b)
    graph.add_node("decision", decision)
    graph.add_node("fix", fix)

    graph.add_edge("start", "source")
    graph.add_parallel_edges("source", ["review_a", "review_b"])
    graph.add_edge("review_a", "decision")
    graph.add_edge("review_b", "decision")
    graph.add_conditional_edges("decision", router, {"fix": "fix", "done": END})
    graph.add_edge("fix", END)

    result = await graph.run({})
    assert result["fixed"] == "true"


@pytest.mark.asyncio
async def test_parallel_execution_with_blocking_calls():
    """Async nodes using asyncio.to_thread run truly in parallel, not sequentially."""
    import time

    graph = StateGraph()
    execution_log: list[tuple[str, float]] = []
    start_time = time.monotonic()

    async def source(state: State) -> State:
        return {}

    def _blocking_work(name: str, duration: float) -> str:
        execution_log.append((f"{name}_start", time.monotonic() - start_time))
        time.sleep(duration)
        execution_log.append((f"{name}_end", time.monotonic() - start_time))
        return f"result_{name}"

    async def parallel_a(state: State) -> State:
        result = await asyncio.to_thread(_blocking_work, "a", 0.1)
        return {"a_out": result}

    async def parallel_b(state: State) -> State:
        result = await asyncio.to_thread(_blocking_work, "b", 0.1)
        return {"b_out": result}

    async def join(state: State) -> State:
        return {"joined": f"{state['a_out']}+{state['b_out']}"}

    graph.add_node("source", source)
    graph.add_node("parallel_a", parallel_a)
    graph.add_node("parallel_b", parallel_b)
    graph.add_node("join", join)

    graph.add_edge("start", "source")
    graph.add_parallel_edges("source", ["parallel_a", "parallel_b"])
    graph.add_edge("parallel_a", "join")
    graph.add_edge("parallel_b", "join")
    graph.add_edge("join", END)

    result = await graph.run({})

    assert result["a_out"] == "result_a"
    assert result["b_out"] == "result_b"
    assert result["joined"] == "result_a+result_b"

    # Verify true parallelism: both should start before either ends.
    # With sequential execution, total time would be ~0.2s.
    # With parallel execution, total time should be ~0.1s.
    log_dict = {name: t for name, t in execution_log}
    a_start, b_start = log_dict["a_start"], log_dict["b_start"]
    a_end, b_end = log_dict["a_end"], log_dict["b_end"]
    # Both tasks should overlap: b starts before a ends (or vice versa)
    assert b_start < a_end or a_start < b_end, "Tasks did not run in parallel"


# --- Task 4: Event callbacks ---


@pytest.mark.asyncio
async def test_event_callbacks():
    """on_node_start and on_node_end fire for each node."""
    graph = StateGraph()
    events: list[tuple[str, str]] = []

    async def node_a(state: State) -> State:
        return {"a": "done"}

    async def node_b(state: State) -> State:
        return {"b": "done"}

    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)

    @graph.on_node_start
    async def on_start(name: str, state: State) -> None:
        events.append(("start", name))

    @graph.on_node_end
    async def on_end(name: str, state: State) -> None:
        events.append(("end", name))

    await graph.run({})
    assert events == [
        ("start", "a"),
        ("end", "a"),
        ("start", "b"),
        ("end", "b"),
    ]


@pytest.mark.asyncio
async def test_error_callback():
    """on_error fires before exception propagates."""
    graph = StateGraph()
    errors: list[tuple[str, str]] = []

    async def failing_node(state: State) -> State:
        raise RuntimeError("boom")

    graph.add_node("fail", failing_node)
    graph.add_edge("start", "fail")

    @graph.on_error
    async def on_err(name: str, error: Exception) -> None:
        errors.append((name, str(error)))

    with pytest.raises(RuntimeError, match="boom"):
        await graph.run({})

    assert errors == [("fail", "boom")]


# --- Task 5: Start node parameter ---


@pytest.mark.asyncio
async def test_start_node():
    """Can start execution at a specific node, skipping earlier ones."""
    graph = StateGraph()
    executed: list[str] = []

    async def node_a(state: State) -> State:
        executed.append("a")
        return {"a": "done"}

    async def node_b(state: State) -> State:
        executed.append("b")
        return {"b": "done"}

    async def node_c(state: State) -> State:
        executed.append("c")
        return {"c": "done"}

    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_node("c", node_c)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "c")
    graph.add_edge("c", END)

    result = await graph.run({"pre": "existing"}, start_node="b")
    assert "a" not in executed
    assert executed == ["b", "c"]
    assert result["pre"] == "existing"
    assert result["b"] == "done"
    assert result["c"] == "done"


@pytest.mark.asyncio
async def test_start_node_invalid():
    """Raises ValueError for unknown start_node."""
    graph = StateGraph()

    with pytest.raises(ValueError, match="not found"):
        await graph.run({}, start_node="nonexistent")


# --- Task 6: Node helpers ---


@pytest.mark.asyncio
async def test_claude_node_template_interpolation():
    """claude_node interpolates state keys into prompt template."""
    with patch("engine._run_cli_subprocess", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = "review result"

        node = claude_node(
            "Review PR {pr_url}. Previous:\n{findings}",
            output_key="review_output",
            model="opus",
        )
        result = await node({"pr_url": "https://github.com/x/y/pull/1", "findings": "none"})

        assert result == {"review_output": "review result"}
        call_args = mock_run.call_args
        # cmd is passed as the first positional argument
        cmd_list = call_args[0][0]
        prompt_idx = cmd_list.index("-p") + 1
        assert "Review PR https://github.com/x/y/pull/1" in cmd_list[prompt_idx]


@pytest.mark.asyncio
async def test_claude_node_error_handling():
    """claude_node raises on subprocess error."""
    with patch("engine._run_cli_subprocess", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("claude crashed")

        node = claude_node("do something", output_key="out")
        with pytest.raises(RuntimeError, match="claude crashed"):
            await node({})


@pytest.mark.asyncio
async def test_python_node_sync():
    """python_node wraps a sync function."""

    def my_fn(state: State) -> State:
        return {"result": state["input"].upper()}

    node = python_node(my_fn)
    result = await node({"input": "hello"})
    assert result == {"result": "HELLO"}


@pytest.mark.asyncio
async def test_python_node_async():
    """python_node passes through an async function."""

    async def my_fn(state: State) -> State:
        return {"result": state["input"].upper()}

    node = python_node(my_fn)
    result = await node({"input": "hello"})
    assert result == {"result": "HELLO"}


@pytest.mark.asyncio
async def test_template_node():
    """template_node interpolates state keys."""
    node = template_node("Hello {name}, your PR is {pr_url}", output_key="greeting")
    result = await node({"name": "Alice", "pr_url": "https://example.com/pr/1"})
    assert result == {"greeting": "Hello Alice, your PR is https://example.com/pr/1"}


@pytest.mark.asyncio
async def test_template_node_missing_keys():
    """template_node returns empty string for missing keys instead of raising."""
    node = template_node("Previous: {previous_findings}\nNew: {name}", output_key="out")
    result = await node({"name": "Alice"})
    assert result == {"out": "Previous: \nNew: Alice"}


# --- shell_node tests ---


# --- to_mermaid() tests ---


def test_to_mermaid_linear_graph():
    """Linear graph produces correct Mermaid diagram."""
    graph = StateGraph()

    graph.add_node("a", _noop)
    graph.add_node("b", _noop)
    graph.add_edge("start", "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)

    diagram = graph.to_mermaid()
    assert "graph TD" in diagram
    assert "start --> a" in diagram
    assert "a --> b" in diagram
    assert "b --> END" in diagram


def test_to_mermaid_conditional_edges():
    """Conditional edges include labels in the diagram."""
    graph = StateGraph()

    graph.add_node("check", _noop)
    graph.add_node("fix", _noop)

    def router(state: dict[str, str]) -> str:
        return "fix"

    graph.add_edge("start", "check")
    graph.add_conditional_edges("check", router, {"fix": "fix", "done": END})
    graph.add_edge("fix", "check")

    diagram = graph.to_mermaid()
    assert "check -->|fix| fix" in diagram
    assert "check -->|done| END" in diagram
    assert "fix --> check" in diagram


def test_to_mermaid_parallel_edges():
    """Parallel edges are represented in the diagram."""
    graph = StateGraph()

    graph.add_node("source", _noop)
    graph.add_node("a", _noop)
    graph.add_node("b", _noop)
    graph.add_node("join", _noop)

    graph.add_edge("start", "source")
    graph.add_parallel_edges("source", ["a", "b"])
    graph.add_edge("a", "join")
    graph.add_edge("b", "join")
    graph.add_edge("join", END)

    diagram = graph.to_mermaid()
    assert "source --> a" in diagram
    assert "source --> b" in diagram
    assert "a --> join" in diagram
    assert "b --> join" in diagram
    assert "join --> END" in diagram


def test_to_mermaid_end_sentinel():
    """END sentinel renders as terminal node."""
    graph = StateGraph()

    graph.add_node("only", _noop)
    graph.add_edge("start", "only")
    graph.add_edge("only", END)

    diagram = graph.to_mermaid()
    assert "only --> END" in diagram


def test_to_mermaid_empty_graph():
    """Empty graph produces minimal diagram."""
    graph = StateGraph()
    diagram = graph.to_mermaid()
    assert "graph TD" in diagram
    lines = [line.strip() for line in diagram.splitlines() if line.strip()]
    assert len(lines) == 1  # only the header


# --- shell_node tests ---


@pytest.mark.asyncio
async def test_shell_node_captures_stdout():
    """shell_node captures stdout as raw text."""
    node = shell_node("echo 'hello world'", output_key="out")
    result = await node({})
    assert result["out"].strip() == "hello world"


@pytest.mark.asyncio
async def test_shell_node_fails_on_nonzero_exit():
    """shell_node raises RuntimeError on non-zero exit code."""
    node = shell_node("sh -c 'echo oops >&2; exit 1'", output_key="out")
    with pytest.raises(RuntimeError, match="Shell command failed"):
        await node({})


@pytest.mark.asyncio
async def test_shell_node_check_false():
    """shell_node with check=False captures output even on non-zero exit."""
    node = shell_node("sh -c 'echo captured; exit 1'", output_key="out", check=False)
    result = await node({})
    assert result["out"].strip() == "captured"


@pytest.mark.asyncio
async def test_shell_node_template_interpolation():
    """shell_node interpolates state keys into the command."""
    node = shell_node("echo '{greeting} {name}'", output_key="out")
    result = await node({"greeting": "hi", "name": "alice"})
    assert result["out"].strip() == "hi alice"


@pytest.mark.asyncio
async def test_shell_node_empty_stdout():
    """shell_node stores empty string for commands with no output."""
    node = shell_node("true", output_key="out")
    result = await node({})
    assert result["out"] == ""


@pytest.mark.asyncio
async def test_shell_node_in_graph():
    """shell_node works as a node in a full graph execution."""
    graph = StateGraph()

    graph.add_node("list", shell_node("echo 'a\nb\nc'", output_key="items"))

    async def count_lines(state: State) -> State:
        return {"count": str(len(state["items"].strip().splitlines()))}

    graph.add_node("count", python_node(count_lines))
    graph.add_edge("start", "list")
    graph.add_edge("list", "count")
    graph.add_edge("count", END)

    result = await graph.run({})
    assert result["count"] == "3"


# --- Default progress logging tests ---


@pytest.mark.asyncio
async def test_default_logging_when_no_callbacks(capsys: pytest.CaptureFixture[str]) -> None:
    """Default [workflow] lines appear on stderr when no callbacks are registered."""
    graph = StateGraph()

    async def node_a(state: State) -> State:
        return {"a": "done"}

    graph.add_node("a", node_a)
    graph.add_edge("start", "a")
    graph.add_edge("a", END)

    await graph.run({})

    captured = capsys.readouterr()
    assert "[workflow:a] Starting" in captured.err
    assert "[workflow:a] Finished" in captured.err


@pytest.mark.asyncio
async def test_default_logging_suppressed_with_callbacks(capsys: pytest.CaptureFixture[str]) -> None:
    """No [workflow] lines appear when on_node_start callback is registered."""
    graph = StateGraph()

    async def node_a(state: State) -> State:
        return {"a": "done"}

    graph.add_node("a", node_a)
    graph.add_edge("start", "a")
    graph.add_edge("a", END)

    @graph.on_node_start
    async def _on_start(name: str, state: State) -> None:
        pass

    await graph.run({})

    captured = capsys.readouterr()
    assert "[workflow]" not in captured.err


@pytest.mark.asyncio
async def test_error_logging_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """[workflow] ERROR line appears on stderr when a node raises."""
    graph = StateGraph()

    async def boom(state: State) -> State:
        raise RuntimeError("kaboom")

    graph.add_node("boom", boom)
    graph.add_edge("start", "boom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await graph.run({})

    captured = capsys.readouterr()
    assert "[workflow:boom] ERROR:" in captured.err


# --- Model detection tests ---


def test_detect_available_models_returns_dict() -> None:
    """detect_available_models returns a dict with claude, codex, gemini keys."""
    result = detect_available_models()
    assert isinstance(result, dict)
    assert "claude" in result
    assert "codex" in result
    assert "gemini" in result
    assert all(isinstance(v, bool) for v in result.values())


def test_detect_available_models_claude_is_available() -> None:
    """Claude CLI should be available in this environment."""
    result = detect_available_models()
    assert result["claude"] is True


# --- _prompt_template attribute tests ---


def test_claude_node_has_prompt_template():
    """claude_node exposes its prompt template as _prompt_template."""
    node = claude_node("Fix this: {error}", output_key="fix")
    assert node._prompt_template == "Fix this: {error}"  # type: ignore[attr-defined]


def test_codex_node_has_prompt_template():
    """codex_node exposes its prompt template as _prompt_template."""
    node = codex_node("Implement: {spec}", output_key="code")
    assert node._prompt_template == "Implement: {spec}"  # type: ignore[attr-defined]


def test_gemini_node_has_prompt_template():
    """gemini_node exposes its prompt template as _prompt_template."""
    node = gemini_node("Summarize: {findings}", output_key="summary")
    assert node._prompt_template == "Summarize: {findings}"  # type: ignore[attr-defined]


def test_shell_node_has_prompt_template():
    """shell_node exposes its command template as _prompt_template."""
    node = shell_node("uv run pytest {test_path}", output_key="test_out")
    assert node._prompt_template == "uv run pytest {test_path}"  # type: ignore[attr-defined]


def test_template_node_has_prompt_template():
    """template_node exposes its template string as _prompt_template."""
    node = template_node("Report: {lint}\n{tests}", output_key="report")
    assert node._prompt_template == "Report: {lint}\n{tests}"  # type: ignore[attr-defined]


def test_template_node_has_diagram_label():
    """template_node has a _diagram_label."""
    node = template_node("Report: {output}", output_key="report")
    assert node._diagram_label == "template"  # type: ignore[attr-defined]
