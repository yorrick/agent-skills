#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mermaid-ascii"]
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
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

# Type aliases
State = dict[str, str]
NodeFn = Callable[[State], Awaitable[State]]
RouterFn = Callable[[State], str]


class _SafeFormatMap(dict[str, str]):
    """Dict subclass that returns empty string for missing keys in format_map.

    This makes template interpolation safe in loops — on the first pass,
    ``{previous_findings}`` resolves to ``""`` instead of raising KeyError.
    """

    def __missing__(self, key: str) -> str:
        return ""


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
    target: str | _EndSentinel


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
        self._node_meta: dict[str, str] = {}  # name -> label for diagram
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
        # Extract diagram label from node metadata if set by node helpers
        label = getattr(fn, "_diagram_label", None)
        if label:
            self._node_meta[name] = label

    def add_edge(self, source: str, target: str | _EndSentinel) -> None:
        """Add an unconditional edge from source to target."""
        self._edges.append(_Edge(source, target))

    def add_conditional_edges(self, source: str, router: RouterFn, route_map: dict[str, str | _EndSentinel]) -> None:
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

    def _get_next_node(self, current: str) -> str | None:
        """Get next node after current. Returns None if END reached.

        Note: Conditional and parallel edges are handled separately in run().
        This method only resolves unconditional edges.
        """
        for edge in self._edges:
            if edge.source == current:
                if edge.target == END or isinstance(edge.target, _EndSentinel):
                    return None
                if isinstance(edge.target, str):
                    return edge.target

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

    def _diagram_id(self, name: str) -> str:
        """Return a diagram-friendly node ID, embedding metadata if present."""
        meta = self._node_meta.get(name)
        if meta:
            # Encode metadata into the ID: "implement" → "implement:codex"
            return f"{name}:{meta}"
        return name

    def to_mermaid(self) -> str:
        """Generate a Mermaid flowchart string from the graph structure.

        Nodes with metadata (model, effort, etc.) get annotated IDs like
        ``implement:codex_default`` so the diagram shows what runs each step.
        """
        lines: list[str] = ["graph TD"]
        did = self._diagram_id

        # Unconditional edges
        for edge in self._edges:
            target = "END" if isinstance(edge.target, _EndSentinel) else did(edge.target)
            lines.append(f"    {did(edge.source)} --> {target}")

        # Conditional edges (with labels)
        for ce in self._conditional_edges:
            for label, target in ce.route_map.items():
                target_name = "END" if isinstance(target, _EndSentinel) else did(target)
                lines.append(f"    {did(ce.source)} -->|{label}| {target_name}")

        # Parallel edges
        for source, targets in self._parallel_edges.items():
            for t in targets:
                lines.append(f"    {did(source)} --> {did(t)}")

        return "\n".join(lines)

    def to_ascii(self) -> str:
        """Render the graph as an ASCII box-and-arrow diagram.

        Uses ``mermaid-ascii`` to convert the Mermaid flowchart into a
        terminal-friendly representation with Unicode box-drawing characters.
        """
        import subprocess as _sp

        mermaid = self.to_mermaid()
        result = _sp.run(
            ["mermaid-ascii"],
            input=mermaid,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return mermaid  # fall back to raw Mermaid
        return result.stdout

    def _find_start_node(self, start_node: str | None) -> str:
        """Find the first node to execute."""
        if start_node is not None:
            if start_node not in self._nodes:
                raise ValueError(f"start_node '{start_node}' not found in graph")
            return start_node
        for edge in self._edges:
            if edge.source == "start" and isinstance(edge.target, str):
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

        current = self._find_start_node(start_node)

        while True:
            # Track visits for loop detection — count full cycles, not per-node re-visits.
            # A node visited N times has been through N-1 complete loop cycles.
            visit_count = visited.get(current, 0) + 1
            visited[current] = visit_count
            if visit_count > self.max_iterations + 1:
                raise MaxIterationsExceeded(f"Exceeded {self.max_iterations} loop iterations at node '{current}'")

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
                results = await asyncio.gather(*[self._run_node(name, state) for name in targets])
                for r in results:
                    state.update(r)
                # Find the join node (node that all parallel targets feed into)
                join = self._find_join_node(targets)
                if join is None:
                    return state
                current = join
                continue

            # 3. Check unconditional edges
            next_node = self._get_next_node(current)
            if next_node is None:
                return state
            current = next_node

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
        data = json.loads(stdout)
        if data.get("is_error"):
            raise RuntimeError(f"CLI error: {data.get('result', 'Unknown error')}")
        return data.get("result", data.get("message", stdout))
    except (json.JSONDecodeError, AttributeError):
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
        prompt = prompt_template.format_map(_SafeFormatMap(state))
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--model", model, "--effort", effort]
        if permission_mode != "default":
            cmd += ["--permission-mode", permission_mode]
        result = await _run_cli_subprocess(cmd, env_strip=["CLAUDECODE", "ANTHROPIC_API_KEY"])
        return {output_key: result}

    _node._diagram_label = f"claude {model}/{effort}"  # type: ignore[attr-defined]
    return _node


def codex_node(
    prompt_template: str,
    output_key: str = "output",
    model: str | None = None,
    sandbox: str = "workspace-write",
    cwd: str | None = None,
) -> NodeFn:
    """Create a node that runs a headless Codex session via ``codex exec``."""

    async def _node(state: State) -> State:
        prompt = prompt_template.format_map(_SafeFormatMap(state))
        cmd = ["codex", "exec", "--sandbox", sandbox]
        if model:
            cmd.extend(["--model", model])
        if cwd:
            cmd.extend(["-C", cwd])
        cmd.append(prompt)
        result = await _run_cli_subprocess(cmd)
        return {output_key: result}

    model_label = model or "default"
    _node._diagram_label = f"codex {model_label}"  # type: ignore[attr-defined]
    return _node


def gemini_node(
    prompt_template: str,
    output_key: str = "output",
) -> NodeFn:
    """Create a node that runs a headless Gemini CLI session."""

    async def _node(state: State) -> State:
        prompt = prompt_template.format_map(_SafeFormatMap(state))
        cmd = ["gemini", "-p", prompt]
        result = await _run_cli_subprocess(cmd)
        return {output_key: result}

    _node._diagram_label = "gemini"  # type: ignore[attr-defined]
    return _node


def python_node(fn: Callable[[State], State] | Callable[[State], Awaitable[State]]) -> NodeFn:
    """Wrap a sync or async function as an async node.

    Sync functions are run in a thread pool via asyncio.to_thread to avoid
    blocking the event loop during long-running subprocess calls.
    """
    if inspect.iscoroutinefunction(fn):
        fn._diagram_label = "python"  # type: ignore[union-attr]
        return fn  # type: ignore[return-value]

    async def _node(state: State) -> State:
        return await asyncio.to_thread(fn, state)  # type: ignore[return-value]

    _node._diagram_label = "python"  # type: ignore[attr-defined]
    return _node


def shell_node(
    command_template: str,
    output_key: str = "output",
    cwd: Path | None = None,
    check: bool = True,
) -> NodeFn:
    """Create a node that runs an arbitrary shell command.

    Interpolates state keys into *command_template* using ``{key}`` syntax,
    then executes via an async subprocess.  Captures stdout as raw text into
    *output_key*.

    When *check* is ``True`` (default), raises ``RuntimeError`` on non-zero
    exit.  Set ``check=False`` to always capture output regardless of exit
    code — useful for commands whose failure is an expected signal (e.g. tests).
    """

    async def _node(state: State) -> State:
        command = command_template.format_map(_SafeFormatMap(state))
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode() if stdout_bytes else ""

        if check and proc.returncode != 0:
            stderr = stderr_bytes.decode() if stderr_bytes else ""
            raise RuntimeError(f"Shell command failed (exit {proc.returncode}): {stderr[:500]}")

        return {output_key: stdout}

    _node._diagram_label = "shell"  # type: ignore[attr-defined]
    return _node


def template_node(template: str, output_key: str = "output") -> NodeFn:
    """Create a node that interpolates state keys into a template string."""

    async def _node(state: State) -> State:
        return {output_key: template.format_map(_SafeFormatMap(state))}

    return _node
