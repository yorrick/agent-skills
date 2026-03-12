# Workflow Engine Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a lightweight async graph execution engine in `dev-loop/scripts/engine.py` and refactor `dev-loop/scripts/dev-loop.py` to use it.

**Architecture:** The engine provides a LangGraph-inspired `StateGraph` builder with `add_node()`, `add_edge()`, `add_conditional_edges()`, `add_parallel_edges()` methods. Nodes are async callables `(dict[str,str]) -> dict[str,str]`. Execution is `asyncio`-based with parallel support via `asyncio.gather()`. Dev-loop.py is refactored to define its workflow as a graph and delegate execution to the engine. **v1 strategy:** Dev-loop nodes use `python_node()` wrappers around the existing `run_claude()` function (preserving file artifacts and the integration test contract). The engine's `claude_node()` helper exists for future consumers and new workflows — migration of dev-loop to `claude_node()` is a future task.

**Tech Stack:** Python 3.10+, asyncio, dataclasses (stdlib only). No external dependencies. Tests use pytest with pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-03-12-workflow-engine-design.md`

---

## Chunk 1: Engine Core

### Task 1: Core types and StateGraph builder

**Files:**
- Create: `dev-loop/scripts/engine.py`
- Create: `dev-loop/tests/test_engine.py`

- [ ] **Step 0: Add test dependencies to pyproject.toml**

Add `pytest` and `pytest-asyncio` to dev dependencies in `/Users/yorrickjansen/work/claude-code-plugins/pyproject.toml`:

```toml
[dependency-groups]
dev = [
    "pyright>=1.1",
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.9",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv sync --group dev`

- [ ] **Step 1: Write failing tests for core types and graph builder**

Create `dev-loop/tests/test_engine.py`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest", "pytest-asyncio"]
# ///
"""Unit tests for the workflow engine."""

from __future__ import annotations

import pytest

# Ensure engine module is importable
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from engine import StateGraph, END, MaxIterationsExceeded


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: FAIL — `engine` module not found.

- [ ] **Step 3: Implement core types and StateGraph builder**

Create `dev-loop/scripts/engine.py`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Lightweight async graph execution engine for orchestrating headless AI coding agents.

Provides a LangGraph-inspired StateGraph builder with support for:
- Async node execution
- Conditional edges with router functions
- Parallel execution groups
- Loop detection with max_iterations safety valve
- Event callbacks for observability
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable


# Type aliases
State = dict[str, str]
NodeFn = Callable[[State], Awaitable[State]]
RouterFn = Callable[[State], str]


class _EndSentinel:
    """Sentinel object representing the end of graph execution."""

    def __repr__(self) -> str:
        return "END"

    def __str__(self) -> str:
        return "END"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EndSentinel)

    def __hash__(self) -> int:
        return hash("__END__")


END = _EndSentinel()


class MaxIterationsExceeded(Exception):
    """Raised when the graph exceeds the maximum number of loop iterations."""


@dataclass
class _Edge:
    source: str
    target: str


@dataclass
class _ConditionalEdge:
    source: str
    router: RouterFn
    route_map: dict[str, str | _EndSentinel]


class StateGraph:
    """Graph builder and async executor for workflow orchestration."""

    def __init__(self, max_iterations: int = 5, cwd: Path | None = None) -> None:
        self.max_iterations = max_iterations
        self.cwd = cwd
        self._nodes: dict[str, NodeFn] = {}
        self._edges: list[_Edge] = []
        self._conditional_edges: list[_ConditionalEdge] = []
        self._parallel_edges: dict[str, list[str]] = {}

        # Event callbacks
        self._on_node_start: list[Callable[..., Awaitable[None]]] = []
        self._on_node_end: list[Callable[..., Awaitable[None]]] = []
        self._on_error: list[Callable[..., Awaitable[None]]] = []

    def add_node(self, name: str, fn: NodeFn) -> None:
        """Register a named node with its async callable."""
        self._nodes[name] = fn

    def add_edge(self, source: str, target: str) -> None:
        """Add an unconditional edge from source to target."""
        self._edges.append(_Edge(source, target))

    def add_conditional_edges(
        self, source: str, router: RouterFn, route_map: dict[str, str | _EndSentinel]
    ) -> None:
        """Add a conditional edge with a router function."""
        self._conditional_edges.append(_ConditionalEdge(source, router, route_map))

    def add_parallel_edges(self, source: str, targets: list[str]) -> None:
        """Add parallel edges from source to multiple targets."""
        self._parallel_edges[source] = targets

    def on_node_start(self, fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """Decorator to register a node_start callback."""
        self._on_node_start.append(fn)
        return fn

    def on_node_end(self, fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """Decorator to register a node_end callback."""
        self._on_node_end.append(fn)
        return fn

    def on_error(self, fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        """Decorator to register an error callback."""
        self._on_error.append(fn)
        return fn
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Run linting and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright dev-loop/scripts/engine.py dev-loop/tests/test_engine.py`
Expected: No errors.

- [ ] **Step 6: Commit**

```bash
git add dev-loop/scripts/engine.py dev-loop/tests/test_engine.py
git commit -m "feat(engine): add core types and StateGraph builder"
```

### Task 2: Graph execution — linear and conditional edges

**Files:**
- Modify: `dev-loop/scripts/engine.py`
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write failing tests for linear and conditional execution**

Append to `dev-loop/tests/test_engine.py`:

```python
import asyncio


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
```

Also add this import at the top of the test file (after the existing imports from engine):

```python
from engine import State
```

- [ ] **Step 2: Run tests to verify the new tests fail**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py::test_linear_execution dev-loop/tests/test_engine.py::test_conditional_execution dev-loop/tests/test_engine.py::test_max_iterations_exceeded -v`
Expected: FAIL — `graph.run()` method not implemented.

- [ ] **Step 3: Implement the `run()` method on StateGraph**

Add to `engine.py` inside the `StateGraph` class:

```python
    def _get_next_nodes(self, current: str) -> list[str] | None:
        """Get next node(s) after current. Returns None if END reached.

        Note: Conditional and parallel edges are handled separately in run().
        This method only resolves unconditional edges.
        """
        for edge in self._edges:
            if edge.source == current:
                if edge.target == END or isinstance(edge.target, _EndSentinel):
                    return None
                return [edge.target]

        return None

    def _get_conditional_target(self, current: str, state: State) -> str | _EndSentinel | None:
        """Evaluate conditional edge router and return target."""
        for ce in self._conditional_edges:
            if ce.source == current:
                label = ce.router(state)
                target = ce.route_map.get(label)
                if target is None:
                    raise ValueError(f"Router for '{current}' returned unknown label '{label}'")
                return target
        return None

    def _find_start_node(self, start_node: str | None) -> str:
        """Find the first node to execute."""
        if start_node:
            if start_node not in self._nodes:
                raise ValueError(f"start_node '{start_node}' not found in graph")
            return start_node
        for edge in self._edges:
            if edge.source == "start":
                return edge.target
        raise ValueError("No edge from 'start' found. Add graph.add_edge('start', '<first_node>')")

    async def _emit_node_start(self, name: str, state: State) -> None:
        for cb in self._on_node_start:
            await cb(name, state)

    async def _emit_node_end(self, name: str, state: State) -> None:
        for cb in self._on_node_end:
            await cb(name, state)

    async def _emit_error(self, name: str, error: Exception) -> None:
        for cb in self._on_error:
            await cb(name, error)

    async def run(self, initial_state: State | None = None, start_node: str | None = None) -> State:
        """Execute the graph starting from the given node or the 'start' edge."""
        state: State = dict(initial_state) if initial_state else {}
        visited: dict[str, int] = {}  # node_name -> visit count
        backward_traversals = 0

        current = self._find_start_node(start_node)

        while True:
            # Track visits for loop detection
            visit_count = visited.get(current, 0)
            if visit_count > 0:
                backward_traversals += 1
                if backward_traversals > self.max_iterations:
                    raise MaxIterationsExceeded(
                        f"Exceeded {self.max_iterations} loop iterations at node '{current}'"
                    )
            visited[current] = visit_count + 1

            # Execute node
            node_fn = self._nodes.get(current)
            if node_fn is None:
                raise ValueError(f"Node '{current}' not found in graph")

            await self._emit_node_start(current, state)
            try:
                result = await node_fn(state)
                state.update(result)
            except Exception as e:
                await self._emit_error(current, e)
                raise
            await self._emit_node_end(current, state)

            # Determine next node
            # 1. Check conditional edges
            conditional_target = self._get_conditional_target(current, state)
            if conditional_target is not None:
                if isinstance(conditional_target, _EndSentinel):
                    return state
                current = conditional_target
                continue

            # 2. Check parallel edges
            if current in self._parallel_edges:
                targets = self._parallel_edges[current]
                # Run all targets concurrently
                results = await asyncio.gather(
                    *[self._run_node(name, state) for name in targets]
                )
                for r in results:
                    state.update(r)
                # Find the join node (node that all parallel targets feed into)
                current = self._find_join_node(targets)
                if current is None:
                    return state
                continue

            # 3. Check unconditional edges
            next_nodes = self._get_next_nodes(current)
            if next_nodes is None:
                return state
            current = next_nodes[0]

    async def _run_node(self, name: str, state: State) -> State:
        """Run a single node and return its output dict."""
        node_fn = self._nodes.get(name)
        if node_fn is None:
            raise ValueError(f"Node '{name}' not found in graph")

        await self._emit_node_start(name, state)
        try:
            result = await node_fn(state)
        except Exception as e:
            await self._emit_error(name, e)
            raise
        await self._emit_node_end(name, {**state, **result})
        return result

    def _find_join_node(self, parallel_targets: list[str]) -> str | None:
        """Find the common successor of parallel targets (join node)."""
        successors: list[set[str]] = []
        for target in parallel_targets:
            target_successors: set[str] = set()
            for edge in self._edges:
                if edge.source == target and not isinstance(edge.target, _EndSentinel):
                    target_successors.add(edge.target)
            for ce in self._conditional_edges:
                if ce.source == target:
                    for t in ce.route_map.values():
                        if not isinstance(t, _EndSentinel):
                            target_successors.add(t)
            successors.append(target_successors)

        if not successors:
            return None

        common = successors[0]
        for s in successors[1:]:
            common = common & s

        if len(common) > 1:
            raise ValueError(f"Ambiguous join: parallel targets {parallel_targets} share multiple successors: {common}")
        if common:
            return common.pop()
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 5: Run linting and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright dev-loop/scripts/engine.py dev-loop/tests/test_engine.py`
Expected: No errors.

- [ ] **Step 6: Commit**

```bash
git add dev-loop/scripts/engine.py dev-loop/tests/test_engine.py
git commit -m "feat(engine): add graph execution with linear, conditional edges, and loop detection"
```

### Task 3: Parallel execution with join semantics

**Files:**
- Modify: `dev-loop/scripts/engine.py`
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write failing tests for parallel execution**

Append to `dev-loop/tests/test_engine.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass** (parallel execution should already work from Task 2's implementation)

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py::test_parallel_execution dev-loop/tests/test_engine.py::test_parallel_then_conditional -v`
Expected: PASS (if the Task 2 implementation handles parallel edges correctly) or FAIL (if adjustments needed).

- [ ] **Step 3: Fix any issues found in parallel execution**

If tests fail, debug and fix the parallel execution in `engine.py`. The key behaviors to verify:
- `asyncio.gather()` runs parallel nodes concurrently
- Join node waits for all predecessors
- State from all parallel branches is merged before join node runs

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Run linting and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright dev-loop/scripts/engine.py dev-loop/tests/test_engine.py`

- [ ] **Step 6: Commit**

```bash
git add dev-loop/scripts/engine.py dev-loop/tests/test_engine.py
git commit -m "feat(engine): add parallel execution with join semantics"
```

### Task 4: Event callbacks

**Files:**
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write tests for event callbacks**

Append to `dev-loop/tests/test_engine.py`:

```python
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
        ("start", "a"), ("end", "a"),
        ("start", "b"), ("end", "b"),
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
```

- [ ] **Step 2: Run tests to verify they pass** (callbacks already implemented in Task 1)

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py::test_event_callbacks dev-loop/tests/test_engine.py::test_error_callback -v`
Expected: PASS.

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/test_engine.py
git commit -m "test(engine): add event callback tests"
```

### Task 5: Start node parameter

**Files:**
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write test for start_node parameter**

Append to `dev-loop/tests/test_engine.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they pass** (start_node already implemented in Task 2)

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py::test_start_node dev-loop/tests/test_engine.py::test_start_node_invalid -v`
Expected: PASS.

- [ ] **Step 3: Run full test suite, linting, and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v && uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright dev-loop/scripts/engine.py dev-loop/tests/test_engine.py`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add dev-loop/tests/test_engine.py
git commit -m "test(engine): add start_node parameter tests"
```

## Chunk 2: Node Helpers

### Task 6: CLI node helpers (claude_node, codex_node, gemini_node)

**Files:**
- Modify: `dev-loop/scripts/engine.py`
- Modify: `dev-loop/tests/test_engine.py`

- [ ] **Step 1: Write failing tests for claude_node**

Append to `dev-loop/tests/test_engine.py`:

```python
from unittest.mock import AsyncMock, patch
from engine import claude_node, python_node, template_node


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
        # prompt is embedded in the cmd list, not as a separate kwarg
        cmd_list = call_args[1]["cmd"]
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py::test_claude_node_template_interpolation dev-loop/tests/test_engine.py::test_claude_node_error_handling -v`
Expected: FAIL — `claude_node` not defined.

- [ ] **Step 3: Implement node helpers**

Add to the bottom of `engine.py`:

```python
# --- CLI subprocess helpers ---

async def _run_cli_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    env_strip: list[str] | None = None,
) -> str:
    """Run a CLI subprocess and return stdout as a string.

    The command's prompt/input should already be in `cmd` (e.g., via `-p`).
    Stdin is closed (DEVNULL). Raises RuntimeError on non-zero exit or
    if the JSON output contains is_error: true.
    """
    import os
    import json as json_mod

    env = {k: v for k, v in os.environ.items() if k not in (env_strip or [])}

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode() if stdout_bytes else ""

    if proc.returncode != 0:
        stderr = stderr_bytes.decode() if stderr_bytes else ""
        raise RuntimeError(f"Subprocess failed (exit {proc.returncode}): {stderr[:500]}")

    # Try to extract result from JSON output
    try:
        data = json_mod.loads(stdout)
        if data.get("is_error"):
            raise RuntimeError(f"CLI error: {data.get('result', 'Unknown error')}")
        return data.get("result", data.get("message", stdout))
    except (json_mod.JSONDecodeError, AttributeError):
        return stdout


def claude_node(
    prompt_template: str,
    output_key: str = "output",
    model: str = "opus",
    effort: str = "high",
    permission_mode: str = "default",
) -> NodeFn:
    """Create a node that runs a headless Claude session."""
    async def _node(state: State) -> State:
        prompt = prompt_template.format_map(state)
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model, "--effort", effort]
        if permission_mode != "default":
            cmd += ["--permission-mode", permission_mode]
        result = await _run_cli_subprocess(
            cmd, env_strip=["CLAUDECODE", "ANTHROPIC_API_KEY"]
        )
        return {output_key: result}

    return _node


def codex_node(
    prompt_template: str,
    output_key: str = "output",
) -> NodeFn:
    """Create a node that runs a headless Codex session."""
    async def _node(state: State) -> State:
        prompt = prompt_template.format_map(state)
        cmd = ["codex", "--quiet", "--full-auto", prompt]
        result = await _run_cli_subprocess(cmd)
        return {output_key: result}

    return _node


def gemini_node(
    prompt_template: str,
    output_key: str = "output",
) -> NodeFn:
    """Create a node that runs a headless Gemini CLI session."""
    async def _node(state: State) -> State:
        prompt = prompt_template.format_map(state)
        cmd = ["gemini", "-p", prompt]
        result = await _run_cli_subprocess(cmd)
        return {output_key: result}

    return _node


def python_node(fn: Callable[[State], State] | Callable[[State], Awaitable[State]]) -> NodeFn:
    """Wrap a sync or async function as an async node."""
    import inspect

    if inspect.iscoroutinefunction(fn):
        return fn  # type: ignore[return-value]

    async def _node(state: State) -> State:
        return fn(state)  # type: ignore[return-value]

    return _node


def template_node(template: str, output_key: str = "output") -> NodeFn:
    """Create a node that interpolates state keys into a template string."""
    async def _node(state: State) -> State:
        return {output_key: template.format_map(state)}

    return _node
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Write tests for python_node and template_node**

Append to `dev-loop/tests/test_engine.py`:

```python
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
```

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Run linting and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/engine.py dev-loop/tests/test_engine.py && uv run pyright dev-loop/scripts/engine.py dev-loop/tests/test_engine.py`
Expected: No errors.

- [ ] **Step 8: Commit**

```bash
git add dev-loop/scripts/engine.py dev-loop/tests/test_engine.py
git commit -m "feat(engine): add CLI node helpers (claude_node, codex_node, gemini_node, python_node, template_node)"
```

## Chunk 3: Refactor dev-loop.py

### Task 7: Refactor dev-loop.py to use the engine

**Files:**
- Modify: `dev-loop/scripts/dev-loop.py`

This is the largest task. The existing `dev-loop.py` has ~1031 lines. We keep all prompt functions, GitHub helpers, `RunContext`, and CLI parsing. We replace the manual orchestration in `main()` with a graph definition.

- [ ] **Step 1: Add engine import and update the shebang/deps**

At the top of `dev-loop.py`, add:

```python
import asyncio
```

And add this import after the existing imports:

```python
# Import engine from same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import StateGraph, END, MaxIterationsExceeded, claude_node, python_node
```

- [ ] **Step 2: Convert prompt functions to template-style**

The existing prompt functions return fully constructed strings. Since some prompts need dynamic state values (like `pr_url`, `previous_findings`), we convert them to accept state dicts. Update each prompt function signature to take `state: dict[str, str]` and read values from the dict.

Key changes:
- `_implementation_prompt(issue_url)` → reads `state["issue_url"]`
- `_pr_creation_prompt(issue_url)` → reads `state["issue_url"]`
- `_security_review_prompt(pr_url, previous_findings)` → reads `state["pr_url"]`, `state.get("previous_security_findings", "")`
- `_decision_prompt(code_review_text, security_review_text, ci_failures)` → reads from state
- `_smoke_test_prompt(issue_url)` → reads `state["issue_url"]`
- `_smoke_test_fix_prompt(issue_url, smoke_test_output)` → reads from state
- `_fix_prompt(pr_url, code_review_text, security_review_text, issue_url, ci_failures)` → reads from state

For each prompt function, create a corresponding `python_node` wrapper that constructs the prompt and calls `run_claude` (reusing the existing `run_claude` function), returning the result into the state dict with an appropriate key.

- [ ] **Step 3: Define the graph in main()**

Replace the manual orchestration logic (the `if args.continue_pr: ... elif not pr_url: ...` block and the `for iteration in range(...)` loop) with a graph definition.

**Important v1 decision:** All Claude-calling nodes use `python_node()` wrappers around the existing synchronous `run_claude()` function. This preserves file artifact writing (JSON output files in `.dev-loop/runs/`) which the integration test depends on. The engine's `claude_node()` helper is available for future consumers but is NOT used by dev-loop in v1.

Create node wrapper functions that use `python_node`. Each node:
- Reads inputs from `state` dict
- Calls existing helpers (`run_claude`, `check_claude_error`, `extract_result`, etc.)
- Returns output dict to merge back into state

```python
def _worktree_setup_node(state: State) -> State:
    """Set up a git worktree for the feature branch."""
    worktree_path = create_worktree_via_claude(
        state["issue_url"], Path(state["work_dir"]) / "worktree-setup.json",
        state.get("permission_mode", "default"),
    )
    return {"worktree_path": str(worktree_path), "cwd": str(worktree_path)}

def _implement_node(state: State) -> State:
    """Run implementation via Claude."""
    cwd = Path(state["cwd"]) if state.get("cwd") else None
    impl_file = run_claude(
        _implementation_prompt(state["issue_url"]),
        Path(state["work_dir"]) / "implementation.json",
        state.get("permission_mode", "default"),
        cwd=cwd,
        model="opus",
        effort="high",
    )
    err = check_claude_error(impl_file)
    if err:
        raise RuntimeError(f"Implementation failed: {err}")
    return {"implementation_output": extract_result(impl_file)}
```

Follow this pattern for every node. Each wraps existing logic from `main()` into a function that reads/writes the state dict. The key nodes and their state interactions:

- `_worktree_setup_node`: writes `worktree_path`, `cwd`
- `_implement_node`: reads `issue_url`, `cwd`; writes `implementation_output`
- `_smoke_test_node`: reads `issue_url`, `cwd`; writes `smoke_test_output`; uses `_smoke_test_prompt()`
- `_smoke_test_fix_node`: reads `issue_url`, `smoke_test_output`, `cwd`
- `_smoke_test_retry_node`: reads `issue_url`, `cwd`; writes `smoke_test_retry_output`
- `_create_pr_node`: reads `issue_url`, `cwd`; writes `pr_url`; calls `gh_assign_self()`
- `_simplify_node`: reads `cwd`; uses `/simplify` prompt
- `_simplify_commit_node`: reads `cwd`; commits and pushes
- `_code_review_node`: reads `pr_url`, `cwd`
- `_security_review_node`: reads `pr_url`, `previous_security_findings`, `cwd`; writes `security_review_output`
- `_wait_for_ci_node`: reads `pr_url`; writes `ci_status`, `ci_failures`
- `_decision_node`: reads `code_review_output`, `security_review_output`, `ci_status`, `ci_failures`; writes `decision_output`. CI failure force-sets decision to "YES".
- `_fix_node`: reads `pr_url`, `code_review_output`, `security_review_output`, `issue_url`, `ci_failures`, `cwd`; writes `fix_output`. Also updates `previous_security_findings` in state for next iteration.

Router functions:
- `_smoke_test_router(state)`: returns "pass" or "fail" based on `smoke_test_output`
- `_decision_router(state)`: returns "fix" or "done" based on `decision_output`

Build the graph:

```python
graph = StateGraph(max_iterations=args.max_iterations)

# Register all nodes
graph.add_node("worktree_setup", python_node(_worktree_setup_node))
graph.add_node("implement", python_node(_implement_node))
graph.add_node("smoke_test", python_node(_smoke_test_node))
graph.add_node("smoke_test_fix", python_node(_smoke_test_fix_node))
graph.add_node("smoke_test_retry", python_node(_smoke_test_retry_node))
graph.add_node("create_pr", python_node(_create_pr_node))
graph.add_node("simplify", python_node(_simplify_node))
graph.add_node("simplify_commit", python_node(_simplify_commit_node))
graph.add_node("code_review", python_node(_code_review_node))
graph.add_node("security_review", python_node(_security_review_node))
graph.add_node("wait_for_ci", python_node(_wait_for_ci_node))
graph.add_node("decision", python_node(_decision_node))
graph.add_node("fix", python_node(_fix_node))

# Phase 1 edges
graph.add_edge("start", "worktree_setup")
graph.add_edge("worktree_setup", "implement")
graph.add_edge("implement", "smoke_test")
graph.add_conditional_edges("smoke_test", _smoke_test_router, {
    "pass": "create_pr",
    "fail": "smoke_test_fix",
})
graph.add_edge("smoke_test_fix", "smoke_test_retry")
graph.add_conditional_edges("smoke_test_retry", _smoke_test_router, {
    "pass": "create_pr",
    "fail": "create_pr",
})
graph.add_edge("create_pr", "simplify")

# Phase 2 edges (review loop)
graph.add_edge("simplify", "simplify_commit")
graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
graph.add_edge("code_review", "wait_for_ci")
graph.add_edge("security_review", "wait_for_ci")
graph.add_edge("wait_for_ci", "decision")
graph.add_conditional_edges("decision", _decision_router, {"fix": "fix", "done": END})
graph.add_edge("fix", "simplify")

# Wire up observability
@graph.on_node_start
async def _on_start(node_name: str, state: State) -> None:
    ctx.status(node_name, "Running")
    ctx.log(f"Starting: {node_name}")

@graph.on_node_end
async def _on_end(node_name: str, state: State) -> None:
    ctx.log(f"Finished: {node_name}")

@graph.on_error
async def _on_err(node_name: str, error: Exception) -> None:
    ctx.log(f"ERROR in {node_name}: {error}")

# Pre-graph validation (same as existing code)
if not check_dependencies():
    return 1
if args.continue_pr and args.review_only:
    print("Error: --continue-pr and --review-only are mutually exclusive", file=sys.stderr)
    return 1
if args.continue_pr:
    branch_result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, timeout=10)
    if branch_result.stdout.strip() in ("main", "master"):
        print(f"Error: --continue-pr cannot be used on '{branch_result.stdout.strip()}'", file=sys.stderr)
        return 1
if args.review_only and not re.match(r"https://github\.com/.+/pull/\d+", args.review_only):
    print(f"Error: invalid GitHub PR URL: {args.review_only}", file=sys.stderr)
    return 1

# Build initial state
initial_state: dict[str, str] = {
    "issue_url": issue_url,
    "work_dir": str(work_dir),
    "permission_mode": permission_mode,
    "max_iterations": str(args.max_iterations),
    "reviewers": args.reviewers,
    "previous_security_findings": "",
}

# Determine start node and mode-specific state
start = None
if args.review_only:
    initial_state["pr_url"] = args.review_only
    initial_state["cwd"] = ""  # use current directory
    start = "simplify"
elif args.continue_pr:
    initial_state["cwd"] = ""  # use current directory
    start = "implement"

try:
    result = asyncio.run(graph.run(initial_state, start_node=start))
    # Post-success actions
    pr_url = result.get("pr_url", "")
    iterations = result.get("iteration_count", "?")
    if args.reviewers and pr_url:
        gh_request_review(extract_pr_number(pr_url), args.reviewers)
    gh_comment(pr_url, (
        "### dev-loop: Review complete\n\n"
        f"No critical issues found after {iterations} iteration(s). "
        "PR is ready for human review."
    ))
    ctx.status("Done", f"No critical issues after {iterations} iterations")
    ctx.log(f"DONE: PR ready after {iterations} iterations")
    ctx.notify(f"PR ready for review after {iterations} iterations")
    ctx.log(f"PR: {pr_url}")
    return 0
except MaxIterationsExceeded:
    # State is lost on exception — post what we can
    ctx.status("Failed", f"Max iterations reached ({args.max_iterations})")
    ctx.log(f"FAILED: Max iterations reached ({args.max_iterations})")
    ctx.notify(f"PR needs manual review ({args.max_iterations} iterations exhausted)")
    return 1
except Exception as e:
    ctx.status("Error", str(e))
    ctx.log(f"ERROR: {e}")
    ctx.notify(f"dev-loop aborted: {e}")
    return 1
```

- [ ] **Step 4: Run linting and type checking**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/dev-loop.py && uv run pyright dev-loop/scripts/dev-loop.py`
Expected: No errors.

- [ ] **Step 5: Run the existing integration test**

Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run dev-loop/tests/test_integration.py`
Expected: Integration test passes — the refactored dev-loop.py should behave identically to the original.

Note: This is a long-running test (~15-45 minutes). It creates a real GitHub repo, runs the full dev-loop, and verifies the results. If it fails, check the artifacts in `.dev-loop/latest/` for debugging.

- [ ] **Step 6: Commit**

```bash
git add dev-loop/scripts/dev-loop.py
git commit -m "refactor(dev-loop): replace manual orchestration with engine graph"
```

### Task 8: Bump plugin version

**Files:**
- Modify: `dev-loop/.claude-plugin/plugin.json`

- [ ] **Step 1: Bump version**

Update `dev-loop/.claude-plugin/plugin.json` version from `"0.21.0"` to `"0.22.0"`.

- [ ] **Step 2: Commit**

```bash
git add dev-loop/.claude-plugin/plugin.json
git commit -m "chore: bump dev-loop version to 0.22.0"
```

## Validation

### Sanity check
- Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pytest dev-loop/tests/test_engine.py -v` — all engine unit tests pass
- Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run ruff check dev-loop/scripts/ dev-loop/tests/` — no lint errors
- Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run pyright dev-loop/scripts/ dev-loop/tests/` — no type errors

### Functional checks
- Run: `cd /Users/yorrickjansen/work/claude-code-plugins && uv run dev-loop/tests/test_integration.py` — full integration test passes, verifying the refactored dev-loop.py works end-to-end
