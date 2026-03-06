#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# ///
"""Post-tool-use hook: run ruff format, ruff check --fix, and pyright on edited Python files."""

import json
import os
import subprocess
import sys
from pathlib import Path


def get_project_root() -> Path:
    if "CLAUDE_PROJECT_DIR" in os.environ:
        return Path(os.environ["CLAUDE_PROJECT_DIR"])
    return Path(__file__).parent.parent.parent


def run_cmd(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=25)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def main() -> None:
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    file_path = input_data.get("tool_input", {}).get("file_path", "")
    if not file_path.endswith(".py"):
        sys.exit(0)

    project_root = get_project_root()

    print(f"Running checks on {file_path}...")

    for label, cmd in [
        ("Formatting", ["uv", "run", "ruff", "format", file_path]),
        ("Linting", ["uv", "run", "ruff", "check", "--fix", file_path]),
        ("Type checking", ["uv", "run", "pyright", file_path]),
    ]:
        print(f"  {label}...")
        rc, stdout, stderr = run_cmd(cmd, project_root)
        if stdout:
            print(stdout)
        if stderr and rc != 0:
            print(stderr)

    print(f"Checks complete for {file_path}")


if __name__ == "__main__":
    main()
