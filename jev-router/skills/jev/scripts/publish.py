"""Push a replay's two results to a private copy of the repository and open a PR
that shows the delegate result against the keep result."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import IDENTITY, run


def run_gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def copy_name(origin_url: str, owner: str) -> str:
    """A private copy, never a fork: a fork of a public repository is public."""
    name = re.sub(r"\.git$", "", re.split(r"[/:]", origin_url.rstrip("/"))[-1])
    return f"{owner}/{name}-jev-replays"


def commit_all(clone: Path, message: str) -> None:
    # Reason: `add -A` never adds ignored files, so the copied .env and
    # dependencies stay out of every published commit.
    run("git", "-C", str(clone), "add", "-A")
    run("git", "-C", str(clone), *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)


def publish(snap: Path, result: dict, *, gh: Callable[..., str] = run_gh, remote: str | None = None) -> str:
    meta = json.loads((snap / "meta.json").read_text())
    sid = result["id"]
    origin = run("git", "-C", meta["toplevel"], "remote", "get-url", "origin").strip()
    copy = copy_name(origin, gh("api", "user", "--jq", ".login").strip())
    try:
        visibility = gh("repo", "view", copy, "--json", "visibility", "--jq", ".visibility").strip()
    except RuntimeError:
        gh("repo", "create", copy, "--private", "--description", "jev-router fork-check replays")
        visibility = "PRIVATE"
    if visibility != "PRIVATE":
        raise RuntimeError(f"{copy} exists and is not private, so nothing was pushed")
    url = remote or f"git@github.com:{copy}.git"
    keep, delegate = Path(result["sides"]["keep"]["clone"]), Path(result["sides"]["delegate"]["clone"])
    for side, clone in (("keep", keep), ("delegate", delegate)):
        commit_all(clone, f"jev fork check {sid}: result")
        run("git", "-C", str(clone), "push", "-q", url, f"HEAD:refs/heads/replay/{sid}/{side}")
    # The comparison commit is built without checking anything out, so neither
    # clone's files change after the replay.
    run("git", "-C", str(keep), "fetch", "-q", str(delegate), "HEAD")
    tree = run("git", "-C", str(keep), "rev-parse", "FETCH_HEAD^{tree}").strip()
    compare = run(
        "git", "-C", str(keep), *IDENTITY, "commit-tree", tree, "-p", "HEAD", "-m", f"jev fork check {sid}: comparison"
    ).strip()
    run("git", "-C", str(keep), "push", "-q", url, f"{compare}:refs/heads/replay/{sid}/compare")
    k, d = result["sides"]["keep"], result["sides"]["delegate"]
    body = (
        f"Fork check {sid}. The base branch holds the keep result; this pull request shows the delegate result "
        f"against it.\n\nKeep: ${k['cost']:.2f}, {k['wall_seconds']:.0f} s, {k['calls']} calls. "
        f"Delegate: ${d['cost']:.2f}, {d['wall_seconds']:.0f} s, {d['calls']} calls"
        f"{'' if d['delegated'] else ', and the session kept the job instead of handing it off'}.\n\n"
        "The job:\n\n" + "\n".join(f"> {line}" for line in (snap / "message.txt").read_text().splitlines())
    )
    return gh(
        "pr",
        "create",
        "--repo",
        copy,
        "--base",
        f"replay/{sid}/keep",
        "--head",
        f"replay/{sid}/compare",
        "--title",
        f"{sid}: keep vs delegate",
        "--body",
        body,
    ).strip()
