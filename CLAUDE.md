# Claude Code Plugins

## Code quality

- **Ruff** for formatting and linting, **pyright** for type checking.
- These run automatically via PostToolUse hook on every Edit/Write of a Python file.
- To run manually: `uv run ruff check .` and `uv run pyright`.
- All Python scripts must use `uv run --script` with inline dependency metadata (PEP 723).
