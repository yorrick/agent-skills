def assert_workflow(graph, h):
    """Bug fix on bare repo: test, fix, verify, commit."""

    # Must have a shell node for running tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Must have an LLM node for fixing
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"

    # Should have conditional routing (test pass/fail decides next step)
    assert len(graph._conditional_edges) > 0, "needs conditional routing for test results"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
