def assert_workflow(graph, h):
    """Feature development on bare repo."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Must have a shell node to run tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Should have a commit step
    assert h.has_node_with_prompt_containing("commit") or h.has_node_with_prompt_containing("git"), (
        "should commit the changes"
    )

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
