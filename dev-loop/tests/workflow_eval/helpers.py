"""GraphInspector — pattern-matching query interface for StateGraph inspection.

All meta methods use substring matching against _diagram_label values in _node_meta.
All prompt methods use case-insensitive substring matching against _prompt_template.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
            # Find nodes that have an edge TO current (unconditional)
            for edge in self.graph._edges:
                if edge.target == current and edge.source not in ancestors:
                    ancestors.add(edge.source)
                    queue.append(edge.source)
            # Parallel edges
            for source, targets in self.graph._parallel_edges.items():
                if current in targets and source not in ancestors:
                    ancestors.add(source)
                    queue.append(source)
            # Conditional edges (target in route_map matches current)
            for ce in self.graph._conditional_edges:
                for target in ce.route_map.values():
                    if target == current and ce.source not in ancestors:
                        ancestors.add(ce.source)
                        queue.append(ce.source)
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
