def assert_workflow(graph, h):
    """Frontend feature with Playwright instructions."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Must reference Playwright in some node's prompt (from CLAUDE.md)
    assert h.has_node_with_prompt_containing("playwright"), (
        "should include playwright testing (from CLAUDE.md instructions)"
    )

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
