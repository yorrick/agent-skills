"""Push a replay's two results to a private copy of the repository and open a PR
that shows the delegate result against the keep result."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import IDENTITY, run

MESSAGE_LIMIT = 2000
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


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
    # `add -A` never adds an ignored file under the CURRENT ignore rules, but the
    # replayed session ran with bypass permissions and could have rewritten those
    # rules or force-added a path; `_refuse_if_ignored_leaked` is what actually
    # keeps a restored ignored file (`.env`, a dependency directory) out, by
    # checking against what `restore` recorded, not against the clone's own
    # (possibly tampered) ignore rules.
    run("git", "-C", str(clone), "add", "-A")
    run("git", "-C", str(clone), *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)


def _refuse_if_unusable(result: dict) -> None:
    """Neither an inconclusive replay nor a side with no priced cost is ever
    publishable: the first has nothing worth comparing, and the second would
    either crash building the PR body or post cost figures that are not real."""
    sid = result.get("id", "?")
    if result.get("inconclusive"):
        raise RuntimeError(f"{sid} is inconclusive ({result.get('reason', '')}); nothing to publish")
    sides = result.get("sides") or {}
    for side in ("keep", "delegate"):
        if side not in sides or "cost" not in sides[side]:
            raise RuntimeError(f"{sid} has no priced cost for {side}; nothing to publish")


def _restored_ignored(clone: Path) -> list[str]:
    """The top-level entries `restore` copied into this clone (from its
    sibling `restore.json`), which must never end up in a published commit no
    matter what the clone's own ignore rules say by the time it is published."""
    info = clone.parent / "restore.json"
    if not info.exists():
        return []
    return json.loads(info.read_text()).get("ignored", [])


def _matches_ignored(path: str, ignored: list[str]) -> str | None:
    for rel in ignored:
        if path == rel or path.startswith(rel + "/"):
            return rel
    return None


def _refuse_if_ignored_leaked(clone: Path, ignored: list[str], label: str) -> None:
    """Refuses if any of `ignored` (or anything under it) is committed at HEAD,
    or was ever touched between `refs/jev/start` (where the replay began) and
    HEAD: a session with bypass permissions can drop a `.gitignore` line or
    force-add a path, so the clone's own current ignore rules cannot be
    trusted; only what `restore` actually copied in can."""
    if not ignored:
        return
    tree = [p for p in run("git", "-C", str(clone), "ls-tree", "-r", "--name-only", "HEAD").splitlines() if p]
    history = [
        p
        for p in run(
            "git", "-C", str(clone), "log", "--name-only", "--pretty=format:", "refs/jev/start..HEAD"
        ).splitlines()
        if p
    ]
    for path in tree + history:
        hit = _matches_ignored(path, ignored)
        if hit:
            raise RuntimeError(f"{hit} would be published from the {label} clone (found as {path}); refusing")


def _refuse_if_branches_exist(url: str, copy: str, sid: str) -> None:
    """A failed publish cannot simply be retried: every attempt stacks a new
    commit, so a second push would be non-fast-forward, and force pushes are
    not allowed. Refuses up front, before any push, with the exact commands to
    clear the copy's branches for a clean retry."""
    existing = run("git", "ls-remote", url, f"refs/heads/replay/{sid}/*")
    if existing.strip():
        raise RuntimeError(
            f"replay/{sid}/* already exists on {copy}; delete it first with: "
            f"gh api -X DELETE repos/{copy}/git/refs/heads/replay/{sid}/keep (and the same for delegate, compare)"
        )


def _pr_body(sid: str, snap: Path, k: dict, d: dict) -> str:
    message = (snap / "message.txt").read_text()
    if len(message) > MESSAGE_LIMIT:
        message = message[:MESSAGE_LIMIT] + "\n...(truncated)"
    return (
        f"Fork check {sid}. The base branch holds the keep result; this pull request shows the delegate result "
        f"against it.\n\nKeep: ${k['cost']:.2f}, {k['wall_seconds']:.0f} s, {k['calls']} calls. "
        f"Delegate: ${d['cost']:.2f}, {d['wall_seconds']:.0f} s, {d['calls']} calls"
        f"{'' if d.get('delegated') else ', and the session kept the job instead of handing it off'}.\n\n"
        "The job:\n\n" + "\n".join(f"> {line}" for line in message.splitlines())
    )


def publish(snap: Path, result: dict, *, gh: Callable[..., str] = run_gh, remote: str | None = None) -> str:
    _refuse_if_unusable(result)
    meta = json.loads((snap / "meta.json").read_text())
    sid = result["id"]
    k, d = result["sides"]["keep"], result["sides"]["delegate"]
    # Built before any gh mutation or push, so a body it cannot build (or a body
    # GitHub would reject) is caught before anything is created or pushed.
    body = _pr_body(sid, snap, k, d)
    origin = run("git", "-C", meta["toplevel"], "remote", "get-url", "origin").strip()
    login = gh("api", "user", "--jq", ".login").strip()
    if not login or not LOGIN_PATTERN.match(login):
        raise RuntimeError(f"gh reported an unusable login: {login!r}")
    copy = copy_name(origin, login)
    try:
        visibility = gh("repo", "view", copy, "--json", "visibility", "--jq", ".visibility").strip()
    except RuntimeError:
        gh("repo", "create", copy, "--private", "--description", "jev-router fork-check replays")
        visibility = "PRIVATE"
    if visibility != "PRIVATE":
        raise RuntimeError(f"{copy} exists and is not private, so nothing was pushed")
    url = remote or f"git@github.com:{copy}.git"
    _refuse_if_branches_exist(url, copy, sid)
    keep, delegate = Path(k["clone"]), Path(d["clone"])
    ignored = {"keep": _restored_ignored(keep), "delegate": _restored_ignored(delegate)}
    for side, clone in (("keep", keep), ("delegate", delegate)):
        commit_all(clone, f"jev fork check {sid}: result")
        _refuse_if_ignored_leaked(clone, ignored[side], side)
    for side, clone in (("keep", keep), ("delegate", delegate)):
        run("git", "-C", str(clone), "push", "-q", url, f"HEAD:refs/heads/replay/{sid}/{side}")
    # The comparison commit is built without checking anything out, so neither
    # clone's files change after the replay.
    run("git", "-C", str(keep), "fetch", "-q", str(delegate), "HEAD")
    tree = run("git", "-C", str(keep), "rev-parse", "FETCH_HEAD^{tree}").strip()
    compare = run(
        "git", "-C", str(keep), *IDENTITY, "commit-tree", tree, "-p", "HEAD", "-m", f"jev fork check {sid}: comparison"
    ).strip()
    run("git", "-C", str(keep), "push", "-q", url, f"{compare}:refs/heads/replay/{sid}/compare")
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
