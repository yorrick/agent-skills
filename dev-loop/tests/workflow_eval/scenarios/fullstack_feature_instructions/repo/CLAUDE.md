# Development Guidelines

## Testing
- Backend: Run `uv run pytest` after any Python change
- Frontend: Run `npx playwright test` after any UI change
- Both test suites must pass before committing

## Quality Gates
- Run `uv run ruff check .` for Python linting
- Run `uv run pyright` for Python type checking
- Run `npm run build` to verify frontend builds
- Always update documentation when adding new API endpoints or pages
