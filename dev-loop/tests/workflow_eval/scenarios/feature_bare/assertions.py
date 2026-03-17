def assert_workflow(graph, h):
    """Feature development on bare repo."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Must have a shell node to run tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Should have conditional routing (test pass/fail decides next step)
    assert len(graph._conditional_edges) > 0, "needs conditional routing for test results"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
