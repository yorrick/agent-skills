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
