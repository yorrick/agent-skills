"""Results go to a private copy, with a PR that shows exactly keep versus delegate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import publish  # noqa: E402
import replay  # noqa: E402
from test_jev_snapshot import git  # noqa: E402


def test_an_existing_public_copy_stops_everything(tmp_path: Path, repo: Path, snap: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")

    def gh(*args: str) -> str:
        if args[:2] == ("api", "user"):
            return "yorrick\n"
        if args[:2] == ("repo", "view"):
            return "PUBLIC\n"
        raise AssertionError(f"unexpected gh call: {args}")

    result = {"id": "x", "sides": {"keep": {"clone": str(tmp_path / "k")}, "delegate": {"clone": str(tmp_path / "d")}}}
    with pytest.raises(RuntimeError, match="not private"):
        publish.publish(snap, result, gh=gh, remote=str(tmp_path / "copy.git"))


def test_copy_name_is_private_repo_named_after_the_source() -> None:
    assert publish.copy_name("git@github.com:acme/shop.git", "yorrick") == "yorrick/shop-jev-replays"
    assert publish.copy_name("https://github.com/acme/shop", "yorrick") == "yorrick/shop-jev-replays"


def test_publish_pushes_both_results_and_opens_the_compare_pr(tmp_path: Path, repo: Path, snap: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    (keep / "RESULT.txt").write_text("keep\n")
    (delegate / "RESULT.txt").write_text("delegate\n")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    calls: list[tuple[str, ...]] = []

    def gh(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("api", "user"):
            return "yorrick\n"
        if args[:2] == ("repo", "view"):
            raise RuntimeError("not found")
        return "https://github.com/yorrick/shop-jev-replays/pull/1\n" if args[:2] == ("pr", "create") else ""

    sid = json.loads((snap / "meta.json").read_text())["id"]
    result = {
        "id": sid,
        "sides": {
            "keep": {"clone": str(keep), "cost": 1.0, "wall_seconds": 60, "calls": 10, "delegated": False},
            "delegate": {"clone": str(delegate), "cost": 0.5, "wall_seconds": 50, "calls": 12, "delegated": True},
        },
    }
    url = publish.publish(snap, result, gh=gh, remote=str(remote))
    assert url == "https://github.com/yorrick/shop-jev-replays/pull/1"
    assert (
        "repo",
        "create",
        "yorrick/shop-jev-replays",
        "--private",
        "--description",
        "jev-router fork-check replays",
    ) in calls
    shown = git(remote, "show", f"replay/{sid}/compare:RESULT.txt")
    assert shown == "delegate\n"
    assert git(remote, "rev-parse", f"replay/{sid}/compare^") == git(remote, "rev-parse", f"replay/{sid}/keep")
    for side in ("keep", "delegate", "compare"):
        assert ".env" not in git(remote, "ls-tree", "-r", "--name-only", f"replay/{sid}/{side}").split()
    assert (keep / "RESULT.txt").read_text() == "keep\n"  # the clone's files are untouched
    pr = next(c for c in calls if c[:2] == ("pr", "create"))
    assert pr[pr.index("--base") + 1] == f"replay/{sid}/keep"
    assert pr[pr.index("--head") + 1] == f"replay/{sid}/compare"
    assert chr(0x2014) not in pr[pr.index("--body") + 1]
