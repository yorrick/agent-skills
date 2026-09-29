"""Fixtures shared across the fork-check test files: a git repo with uncommitted,
untracked and ignored files, and a snapshot taken of it. Every test also runs
with its own Claude Code home, and fails if it wrote into the real one."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import snapshot  # noqa: E402
from test_jev_snapshot import EVENT, git, payload, write  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402

# Reason: read once at import, before any test can monkeypatch Path.home or HOME.
REAL_PROJECTS = Path.home() / ".claude" / "projects"


def _real_project_names() -> set[str]:
    """Names only: the guard never reads what is inside the user's sessions."""
    return {p.name for p in REAL_PROJECTS.iterdir()} if REAL_PROJECTS.is_dir() else set()


@pytest.fixture(scope="session")
def _real_projects_seen(tmp_path_factory: pytest.TempPathFactory) -> tuple[set[str], str]:
    """The real `~/.claude/projects` listing when the session starts, and the
    name prefix any folder a test wrote there would carry: Claude Code names a
    project folder after its working directory, and every test's working copy
    and clone lives under this session's pytest temp root."""
    return _real_project_names(), re.sub(r"[^A-Za-z0-9-]", "-", str(tmp_path_factory.getbasetemp()))


@pytest.fixture(autouse=True)
def _isolated_claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own Claude Code home. `fork_check replay` uses
    CLAUDE_CONFIG_DIR when set and the real `~/.claude` otherwise, so without
    this a replay test installs sessions (fake usage that cost tools would
    count as real spend) into the user's own projects folder. A test that
    passes its own home, or sets the variable itself, still wins."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_claude_projects(_real_projects_seen: tuple[set[str], str]) -> Iterator[None]:
    """Fails the test that made a new folder under the real `~/.claude/projects`.
    Only names under this session's temp root count, so a real Claude Code
    session that starts in a new directory while the suite runs is never
    blamed on a test."""
    yield
    seen, prefix = _real_projects_seen
    new = sorted(name for name in _real_project_names() - seen if name.startswith(prefix))
    seen.update(new)
    if new:
        pytest.fail(f"this test wrote into the real {REAL_PROJECTS}: {new}")


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
