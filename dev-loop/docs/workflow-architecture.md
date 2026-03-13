# Workflow Engine Architecture

## Graph Execution Flow

```mermaid
graph TD
    START((start)) --> worktree_setup
    worktree_setup --> implement
    implement --> smoke_test

    smoke_test -->|pass| create_pr
    smoke_test -->|pass_continue| continue_pr_push
    smoke_test -->|fail| smoke_test_fix

    smoke_test_fix --> smoke_test_retry
    smoke_test_retry -->|default| create_pr
    smoke_test_retry -->|continue_pr| continue_pr_push

    create_pr --> simplify
    continue_pr_push --> simplify

    subgraph "Review Loop"
        simplify --> simplify_commit
        simplify_commit -->|parallel| code_review
        simplify_commit -->|parallel| security_review
        code_review --> wait_for_ci
        security_review --> wait_for_ci
        wait_for_ci --> decision
        decision -->|fix| fix
        fix --> simplify
    end

    decision -->|done| END((END))
```

## Entry Points

| Mode | Start Node | Description |
|------|-----------|-------------|
| Default | `worktree_setup` | Creates worktree, implements, creates PR, reviews |
| `--continue-pr` | `implement` | Implements in current dir, pushes to existing PR, reviews |
| `--review-only` | `simplify` | Reviews an existing PR (no implementation) |

## Engine Components

```mermaid
classDiagram
    class StateGraph {
        +add_node(name, fn)
        +add_edge(source, target)
        +add_conditional_edges(source, router, route_map)
        +add_parallel_edges(source, targets)
        +on_node_start(callback)
        +on_node_end(callback)
        +on_error(callback)
        +run(initial_state, start_node) State
    }

    class NodeHelpers {
        +claude_node(prompt_template, output_key, model)
        +codex_node(prompt_template, output_key)
        +gemini_node(prompt_template, output_key)
        +python_node(fn)
        +template_node(template, output_key)
    }

    StateGraph --> NodeHelpers : uses
```
