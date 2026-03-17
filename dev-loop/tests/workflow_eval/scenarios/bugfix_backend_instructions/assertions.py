def assert_workflow(graph, h):
    """Bug fix on backend repo with CLAUDE.md instructions."""

    # Same core assertions as bugfix_bare
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"
    assert h.has_conditional_loop(), "test→fix loop expected"
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # CLAUDE.md says to run lint and typecheck — should pick those up
    assert h.has_node_with_prompt_containing("ruff") or h.has_node_with_prompt_containing("lint"), (
        "should include linting (from CLAUDE.md instructions)"
    )
    assert h.has_node_with_prompt_containing("pyright") or h.has_node_with_prompt_containing("type"), (
        "should include type checking (from CLAUDE.md instructions)"
    )
