# dev-loop plugin

## Design philosophy

- **Compose, don't reinvent.** Reuse existing commands and skills maintained by others (`/security-review`, `/simplify`, `/code-review:code-review`, `superpowers:*`). Other people are better at writing those — our job is to identify great tools and define workflows that combine them effectively.
- **Workflow orchestration is the value.** The dev-loop plugin defines the sequence, decision gates, and feedback loops. The individual steps are delegated to best-in-class commands/skills.
- **Headless-compatible.** All prompts sent to `claude -p` must work without user interaction. Explicitly disable interactive skills (e.g. `finishing-a-development-branch`) in headless sessions.

## Development guidelines

- All scripts in `scripts/` must use `uv run --script` with inline dependency metadata (PEP 723).
- No bash scripts — use Python with uv inline deps instead.
- The shebang for scripts should be `#!/usr/bin/env -S uv run --script`.
- Scripts must not require a virtual environment or pre-installed packages beyond Python 3.10+.
- Run `uv run ruff check` and `uv run pyright` from the repo root after any Python change.
- After any change to the dev-loop, run the integration test: `uv run dev-loop/tests/test_integration.py`
- When updating dev-loop functionality, always update the integration test accordingly to cover the new/changed behavior.

## Testing requirements for `/workflow` (StateGraph engine)

Every feature of the workflow engine must be covered by tests:

- **Unit tests** (`tests/test_engine.py`): For engine internals — graph construction, execution order, conditional/parallel edges, callbacks, logging behavior, model detection, node helpers (`claude_node`, `shell_node`, `python_node`, `template_node`), and diagram generation. Run with `uv run pytest dev-loop/tests/test_engine.py`.
- **Integration tests** (`tests/test_workflow_integration.py`): For end-to-end workflow execution — scaffolds a real project, generates a multi-LLM workflow script, runs it, and verifies implementation correctness, test results, git state, and progress logging. Run with `uv run dev-loop/tests/test_workflow_integration.py`.
- When adding a new engine feature, add unit tests at minimum. If the feature affects end-to-end workflow behavior (e.g. progress logging, execution flow), also add assertions to the integration test.

## Workflow eval tests

Eval tests verify that the `/workflow` skill generates structurally sound workflows. They call headless Claude to generate a workflow script, import the graph, and run Python assertions.

- Run all evals: `uv run pytest -m eval -v`
- Run a specific scenario: `uv run pytest -m eval -k bugfix_bare -v -s`
- Scenarios live in `tests/workflow_eval/scenarios/` — each is a self-contained directory
- Adding a scenario: create a new directory with `scenario.toml`, `repo/`, and `assertions.py`
- Generated scripts are saved to `tests/workflow_eval/results/` for manual inspection
- These tests make LLM calls — run them when iterating on the `/workflow` skill prompt, not on every commit
