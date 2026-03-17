def assert_workflow(graph, h):
    """Feature dev on backend repo with quality gate instructions."""

    # Core: implement + test + commit
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to implement"
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"
    assert h.has_conditional_loop(), "test→fix loop expected"
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # CLAUDE.md quality gates should be picked up
    assert h.has_node_with_prompt_containing("ruff") or h.has_node_with_prompt_containing("lint"), (
        "should include linting (from CLAUDE.md)"
    )
    assert h.has_node_with_prompt_containing("pyright") or h.has_node_with_prompt_containing("type"), (
        "should include type checking (from CLAUDE.md)"
    )
