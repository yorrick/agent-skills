# Claude Code Plugins

## Code quality

- **Ruff** for formatting and linting, **pyright** for type checking.
- These run automatically via PostToolUse hook on every Edit/Write of a Python file.
- To run manually: `uv run ruff check .` and `uv run pyright`.
- All Python scripts must use `uv run --script` with inline dependency metadata (PEP 723).

## Validation checklist (every change)

- **Linting**: `uv run ruff check .` must pass with no errors.
- **Formatting**: `uv run ruff format --check .` must pass.
- **Type checking**: `uv run pyright` must pass.
- All three checks must pass before claiming any change is complete.

## Documentation

- For every change, assess whether documentation needs updating (README, skill descriptions, CLAUDE.md, inline docs).
- If docs are affected, update them as part of the same change — do not defer.

## Development workflow

- Use `/brainstorm` to explore requirements and write plans. Always use **Opus model** with **max effort** for brainstorming (`claude --model opus --effort max`).
- Execute plans with `/workflow` to orchestrate multi-step implementation.

## Review

- Depending on complexity, include relevant review steps in the workflow: `/simplify`, `/code-review:code-review`, `/security-review`, doc updates, etc.
- For small changes a single review pass may suffice; for larger changes, combine multiple review steps.

## Issue tracking

- Issues are managed in **GitHub Issues** on this repository.
- Whenever you spot something that could be improved (code, docs, tooling, workflow), create a GitHub issue to track it.
