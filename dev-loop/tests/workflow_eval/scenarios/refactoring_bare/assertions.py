def assert_workflow(graph, h):
    """Refactoring on bare repo: extract module, run existing tests."""

    # Must have an LLM node for the refactoring
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to refactor"

    # Must run existing tests to verify refactoring didn't break anything
    assert h.has_node_with_meta("shell"), "needs a shell node to run existing tests"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
