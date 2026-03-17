def assert_workflow(graph, h):
    """Bug fix on backend repo with CLAUDE.md instructions."""

    # Core: shell for tests, LLM for fix, conditional routing
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"
    assert len(graph._conditional_edges) > 0, "needs conditional routing for test results"
    assert h.has_edge_to_end(), "graph must have an edge to END"

    # CLAUDE.md says to run lint and typecheck — should appear somewhere
    # (as separate shell nodes OR baked into an LLM node's prompt)
    has_lint = h.has_node_with_prompt_containing("ruff") or h.has_node_with_prompt_containing("lint")
    has_typecheck = (
        h.has_node_with_prompt_containing("pyright")
        or h.has_node_with_prompt_containing("type check")
        or h.has_node_with_prompt_containing("typecheck")
    )
    assert has_lint, "should include linting (from CLAUDE.md instructions)"
    assert has_typecheck, "should include type checking (from CLAUDE.md instructions)"
