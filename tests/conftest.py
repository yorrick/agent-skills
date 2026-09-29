"""Fixtures shared across the fork-check test files: a git repo with uncommitted,
untracked and ignored files, and a snapshot taken of it."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import snapshot  # noqa: E402
from test_jev_snapshot import EVENT, git, payload, write  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work" / "app"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / ".gitignore").write_text("node_modules/\n.venv/\n.env\n__pycache__/\n")
    (r / "app.py").write_text("print('v1')\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    (r / "app.py").write_text("print('v2')\n")  # uncommitted change
    (r / "notes.md").write_text("draft\n")  # untracked
    (r / "node_modules").mkdir()
    (r / "node_modules" / ".package-lock.json").write_text("{}")
    (r / ".env").write_text("TOKEN=x\n")
    return r


@pytest.fixture
def snap(tmp_path: Path, repo: Path) -> Path:
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text=f"Edited {repo}/app.py")])
    sid = snapshot.take_snapshot(
        tmp_path / "fc", payload(repo, transcript), "build it", "NOTE `jev-router:large`", EVENT
    )
    return tmp_path / "fc" / "snapshots" / sid
