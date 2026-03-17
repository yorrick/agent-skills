# Workflow Generation Eval Framework

## Problem

The `/workflow` skill generates Python workflow scripts via LLM. When iterating on the skill's prompt, there's no way to systematically verify that the generated workflows make good decisions — right graph topology, appropriate model selection, correct use of parallel execution, sensitivity to repo context (CLAUDE.md instructions).

## Goal

A scenario-based eval framework that:

1. Presents a natural language task + a scaffolded repo to headless Claude
2. Claude generates a workflow.py using the `/workflow` skill
3. The generated graph is imported and inspected with Python assertions
4. No workflow execution — only structural inspection

This enables prompt iteration: change the skill, re-run evals, check if the LLM still makes good decisions.

## Design

### Engine changes

#### 1. Expose prompt templates on node functions

Add a `_prompt_template` attribute to `claude_node`, `codex_node`, `gemini_node`, `shell_node`, and `template_node` — same pattern as the existing `_diagram_label`. This lets assertions inspect what prompt or command each node uses.

```python
# In claude_node:
_node._prompt_template = prompt_template

# In shell_node:
_node._prompt_template = command_template

# In template_node:
_node._prompt_template = template
```

`python_node` does not get `_prompt_template` since it wraps arbitrary functions with no template string.

#### 2. Require `build_graph()` in generated scripts

Update the `/workflow` skill (commands/workflow.md) to require that generated scripts put graph construction in a `build_graph()` function. This makes the graph importable for inspection without execution.

`build_graph()` accepts an optional `models` parameter (defaulting to `detect_available_models()`) so that callers can control model availability for deterministic graph topology:

```python
def build_graph(models: dict[str, bool] | None = None) -> StateGraph:
    if models is None:
        models = detect_available_models()
    HAS_CODEX = models["codex"]
    HAS_GEMINI = models["gemini"]

    graph = StateGraph(max_iterations=5)
    # ... add nodes, edges, using HAS_CODEX/HAS_GEMINI for node selection ...
    return graph

if __name__ == "__main__":
    graph = build_graph()
    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        sys.exit(0)
    initial_state = {"work_dir": os.getcwd(), ...}
    asyncio.run(graph.run(initial_state))
```

All graph topology decisions (node types, edges, parallel groups) must happen inside `build_graph()`. The `if __name__` block only handles `initial_state` construction and execution.

### Directory structure

```
dev-loop/tests/workflow_eval/
├── helpers.py               # GraphInspector with pattern-matching query methods
├── test_scenarios.py         # Pytest runner: discover, generate, import, assert
└── scenarios/
    ├── bugfix_bare/
    │   ├── scenario.toml     # name + prompt
    │   ├── repo/             # actual files copied to temp dir
    │   └── assertions.py     # assert_workflow(graph, inspector)
    ├── bugfix_backend_instructions/
    │   ├── scenario.toml
    │   ├── repo/
    │   │   ├── src/...
    │   │   └── CLAUDE.md
    │   └── assertions.py
    ├── feature_bare/
    ├── feature_backend_instructions/
    ├── frontend_feature_instructions/
    ├── fullstack_feature_instructions/
    └── refactoring_bare/
```

### Scenario format

#### `scenario.toml`

Uses TOML (stdlib `tomllib` since Python 3.11, no external dependency):

```toml
name = "Bug fix on bare repo"
prompt = """
Fix the calculate_average function in src/mathlib.py —
it crashes on empty lists with a ZeroDivisionError
"""
```

Minimal — just name and prompt. Repo context (including CLAUDE.md if any) lives as actual files in the `repo/` directory.

#### `assertions.py`

```python
def assert_workflow(graph, h):
    """Assertions for: bug fix on a bare repo (no CLAUDE.md)."""

    # Should have a test runner node
    assert h.has_node_with_meta("shell"), "needs a shell node to run tests"

    # Should have a fix node using an LLM
    assert h.has_node_with_meta("claude", "codex"), "needs an LLM node to fix the bug"

    # Test failure should loop back to fix
    assert h.has_conditional_loop(meta_contains="shell"), "test→fix loop expected"

    # Should terminate
    assert h.has_edge_to_end(), "graph must terminate"

    # Bare repo — should NOT have doc update or lint steps
    assert not h.has_node_with_prompt_containing("doc"), "no doc step expected on bare repo"
```

### GraphInspector (`helpers.py`)

A query interface over the StateGraph internals. All methods operate on the graph's internal data structures (`_nodes`, `_node_meta`, `_edges`, `_conditional_edges`, `_parallel_edges`).

**Meta pattern matching:** All `*_meta` methods use **substring matching** against the `_diagram_label` values stored in `_node_meta`. For example, `"claude"` matches `"claude opus/high"`, `"shell"` matches `"shell"`, `"codex"` matches `"codex default"`. This makes assertions resilient to model/effort variations.

**Prompt pattern matching:** All `*_prompt_containing` methods use **case-insensitive substring matching** against the `_prompt_template` attribute set on node functions by the engine's node helpers.

```python
class GraphInspector:
    """Pattern-matching query interface for StateGraph inspection."""

    def __init__(self, graph: StateGraph):
        self.graph = graph

    def has_node_with_meta(self, *patterns: str) -> bool:
        """True if any node's _node_meta value contains one of the patterns.

        Example: has_node_with_meta("shell") — checks for a shell node.
        Example: has_node_with_meta("claude", "codex") — checks for either.
        """

    def get_nodes_with_meta(self, pattern: str) -> list[str]:
        """Return node names whose _node_meta value contains pattern."""

    def has_node_with_prompt_containing(self, keyword: str) -> bool:
        """True if any node's _prompt_template contains keyword (case-insensitive)."""

    def get_nodes_with_prompt_containing(self, keyword: str) -> list[str]:
        """Return node names whose _prompt_template contains keyword."""

    def has_parallel_group(self) -> bool:
        """True if the graph uses add_parallel_edges."""

    def has_parallel_group_containing(self, *meta_patterns: str) -> bool:
        """True if any parallel group's targets include nodes matching the patterns."""

    def has_conditional_loop(self, meta_contains: str | None = None) -> bool:
        """True if a conditional edge creates a cycle in the graph.

        Algorithm: for each conditional edge, check if any of its route_map
        targets is an ancestor of its source — i.e., the target appears as a
        source in any edge chain that leads to the conditional edge's source.
        Simplified approach: check if any route_map target also appears as a
        source of an edge (unconditional or parallel) earlier in the graph,
        creating a back-edge.

        If meta_contains is provided, only considers conditional edges where
        the source node's meta contains the pattern.
        """

    def node_count(self) -> int:
        """Total number of nodes in the graph."""

    def get_edge_targets(self, source_pattern: str) -> list[str]:
        """All targets reachable from nodes whose name contains source_pattern.

        Checks unconditional, conditional, and parallel edges.
        """

    def has_edge_to_end(self) -> bool:
        """True if the graph has at least one edge to END."""

    def get_model_for_node(self, node_name_pattern: str) -> str | None:
        """Return the _node_meta (model info) for the first node matching pattern."""
```

### Test runner (`test_scenarios.py`)

#### `generate_workflow`

The core mechanism. Reads the `/workflow` skill content (commands/workflow.md), constructs a prompt that includes the skill instructions and the scenario's prompt, then runs headless Claude:

```bash
claude -p "<combined_prompt>" \
    --model sonnet \
    --permission-mode bypassPermissions \
    --cwd <repo_path>
```

Claude runs with the scaffolded repo as cwd, so it picks up any CLAUDE.md in the repo. It generates a `/tmp/workflow_*.py` file. The test locates it by globbing `/tmp/workflow_*.py` (filtered by modification time to avoid collisions).

The combined prompt includes:
1. The full workflow.md skill content (so Claude knows how to generate workflows)
2. The scenario's prompt (the task to generate a workflow for)
3. An instruction to generate the workflow script (not execute it)

#### `import_graph`

Imports the generated script's `build_graph()` function and returns the graph:

1. Add the engine module's directory to `sys.path` so the script's `from engine import ...` resolves
2. Set `CLAUDE_PLUGIN_ROOT` environment variable to the dev-loop plugin directory (so `${CLAUDE_PLUGIN_ROOT}/scripts` resolves in the generated script's path manipulation)
3. Use `importlib.util.spec_from_file_location` to import the generated script
4. Call `module.build_graph(models={"claude": True, "codex": True, "gemini": True})` — pass all models as available for deterministic topology
5. Return the graph

#### Runner

```python
import tomllib

def discover_scenarios() -> list[Path]:
    """Find all scenarios/*/scenario.toml, return parent dirs."""

@pytest.mark.parametrize(
    "scenario_dir",
    discover_scenarios(),
    ids=lambda p: p.name,
)
def test_workflow_scenario(scenario_dir: Path, tmp_path: Path):
    with open(scenario_dir / "scenario.toml", "rb") as f:
        config = tomllib.load(f)

    repo_path = scaffold_repo(scenario_dir, tmp_path)
    workflow_path = generate_workflow(config["prompt"], repo_path)
    graph = import_graph(workflow_path)
    inspector = GraphInspector(graph)

    # Load and run scenario-specific assertions
    spec = importlib.util.spec_from_file_location(
        "assertions", scenario_dir / "assertions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.assert_workflow(graph, inspector)
```

### Scenarios

| # | Directory | Task | CLAUDE.md? | Key assertions |
|---|-----------|------|------------|----------------|
| 1 | `bugfix_bare` | Fix crashing function | No | test→fix loop, shell node for tests, minimal nodes, no unnecessary steps |
| 2 | `bugfix_backend_instructions` | Fix crashing function | Yes: pytest, ruff, update docs | Same loop + lint/typecheck/doc nodes from instructions |
| 3 | `feature_bare` | Add new API endpoint | No | Implement node, test node, commit node |
| 4 | `feature_backend_instructions` | Add new API endpoint | Yes: pytest, ruff, pydantic | Quality gates picked up from CLAUDE.md |
| 5 | `frontend_feature_instructions` | Add settings page | Yes: playwright, component lib | Playwright in test node prompt, appropriate models |
| 6 | `fullstack_feature_instructions` | User profile (API + UI) | Yes: pytest + playwright, docs | Both test types present, parallel where sensible |
| 7 | `refactoring_bare` | Extract class from module | No | Runs existing tests, review step, no new test creation step |

### Repo scaffolds

Each scenario's `repo/` contains a minimal but realistic project structure:

- **Python backend repos**: `src/` with a module, `tests/` with a test file, `pyproject.toml`
- **Frontend repos**: `src/components/`, `tests/e2e/`, `package.json`, `playwright.config.ts`
- **Full-stack repos**: both of the above
- **CLAUDE.md** (when present): contains specific instructions the workflow should pick up

The repos should have enough structure for the LLM to understand the project type, but stay minimal — just enough files to convey context.

### Running the evals

```bash
# Run all scenarios (slow — one LLM call per scenario)
uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -v

# Run a specific scenario
uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -k bugfix_bare -v

# Run with output capture disabled to see Claude's generation
uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -k bugfix_bare -v -s
```

These are manual evals, not CI. Run them when iterating on the `/workflow` skill prompt.

### Dealing with non-determinism

LLM outputs vary between runs. To handle this:

- **Assertions should be loose** — check for structural properties ("has a shell node", "has a conditional loop") not exact node names or counts.
- **Run multiple times when evaluating prompt changes** — run each scenario 3 times. A scenario is "broken" only if it fails consistently (2+ out of 3). A single failure may be LLM variance.
- **Save generated scripts** — when a run produces a particularly good or bad workflow, save the generated script for manual inspection. The test runner should copy generated workflow files to a `results/` directory with timestamps for later review.

### What this does NOT cover

- **Execution correctness**: We don't run the workflows. That's what `test_workflow_integration.py` already does.
- **Prompt content quality**: We check that prompts mention the right keywords (e.g., "playwright"), but not that they're well-written.
