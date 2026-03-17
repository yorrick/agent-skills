# Development Guidelines

## Quality Gates
- Always run `uv run pytest` after any code change
- Always run `uv run ruff check .` for linting
- Always run `uv run pyright` for type checking
- Update documentation in docs/ when changing public APIs
