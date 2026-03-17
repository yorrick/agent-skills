def assert_workflow(graph, h):
    """Full-stack feature with both pytest and playwright."""

    # Must have an LLM node for implementation
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"

    # Should reference both test frameworks (from CLAUDE.md)
    assert h.has_node_with_prompt_containing("pytest") or h.has_node_with_prompt_containing("uv run pytest"), (
        "should include pytest for backend (from CLAUDE.md)"
    )
    assert h.has_node_with_prompt_containing("playwright"), "should include playwright for frontend (from CLAUDE.md)"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"
