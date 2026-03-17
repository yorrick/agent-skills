def assert_workflow(graph, h):
    """Bug fix on bare repo: minimal test→fix loop."""

    # Must have a shell node for running tests
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Must have an LLM node for fixing
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"

    # Test failure should loop back to fix (conditional back-edge)
    assert h.has_conditional_loop(), "test→fix loop expected"

    # Graph must terminate
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # Bare repo — no doc update, lint, or typecheck steps expected
    assert not h.has_node_with_prompt_containing("documentation"), "no doc step expected on bare repo"
