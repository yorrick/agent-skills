# dev-loop plugin

## Development guidelines

- All scripts in `scripts/` must use `uv run --script` with inline dependency metadata (PEP 723).
- No bash scripts — use Python with uv inline deps instead.
- The shebang for scripts should be `#!/usr/bin/env -S uv run --script`.
- Scripts must not require a virtual environment or pre-installed packages beyond Python 3.10+.
- Run `uv run ruff check` and `uv run pyright` from the repo root after any Python change.
