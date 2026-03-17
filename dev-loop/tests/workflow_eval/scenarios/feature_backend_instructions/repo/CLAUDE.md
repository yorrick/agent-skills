# Development Guidelines

## Quality Gates
- Always run `uv run pytest` after any code change
- Always run `uv run ruff check .` for linting
- Always run `uv run pyright` for type checking
- Use pydantic models for any data validation
- Update documentation in docs/ when adding new public functions
