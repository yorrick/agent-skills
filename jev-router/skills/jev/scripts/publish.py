"""Push a replay's two results to a private copy of the repository and open a PR
that shows the delegate result against the keep result."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import IDENTITY, refuse_if_ignored_leaked, refuse_if_unusable, restored_ignored, run, run_on_source

LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
# Reason: the clone's own git hooks (husky, pre-commit) could reformat the result
# after the judge saw it, or reject the commit or the push outright. Used on the
# commit, commit-tree, fetch and both pushes: skipping pre-push hooks too was
# accepted in Ruling F11.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


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
    # rules or force-added a path; `refuse_if_ignored_leaked` (shared with the
    # judge, in replay.py) is what actually keeps a restored ignored file
    # (`.env`, a dependency directory) out, by checking against what `restore`
    # recorded, not against the clone's own (possibly tampered) ignore rules.
    run("git", "-C", str(clone), "add", "-A")
    run("git", "-C", str(clone), *NO_HOOKS, *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)


def _refuse_if_branches_exist(url: str, copy: str, sid: str) -> None:
    """A failed publish cannot simply be retried: every attempt stacks a new
    commit, so a second push would be non-fast-forward, and force pushes are
    not allowed. Refuses up front, before any push, with the exact command to
    delete each ref `ls-remote` actually found (never a generic hint), so a
    clean retry only needs the reported refs removed and nothing else."""
    existing = run("git", "ls-remote", url, f"refs/heads/replay/{sid}/*")
    refs = [line.split("\t", 1)[1] for line in existing.splitlines() if line.strip()]
    if refs:
        commands = "; ".join(f"gh api -X DELETE repos/{copy}/git/{ref}" for ref in refs)
        raise RuntimeError(f"replay/{sid}/* already exists on {copy}; delete it first with: {commands}")


def _pr_body(sid: str, snap: Path, k: dict, d: dict) -> str:
    """Numbers and the snapshot id only. The job's own message is never quoted:
    a message can hold a credential, and the body is published."""
    return (
        f"Fork check {sid}. The base branch holds the keep result; this pull request shows the delegate result "
        f"against it.\n\nKeep: ${k['cost']:.2f}, {k['wall_seconds']:.0f} s, {k['calls']} calls. "
        f"Delegate: ${d['cost']:.2f}, {d['wall_seconds']:.0f} s, {d['calls']} calls"
        f"{'' if d.get('delegated') else ', and the session kept the job instead of handing it off'}.\n\n"
        f"The job's message stays on the machine that ran the check, in the local snapshot folder: "
        f"{snap / 'message.txt'}"
    )


def publish(snap: Path, result: dict, *, gh: Callable[..., str] = run_gh, remote: str | None = None) -> str:
    refuse_if_unusable(result)
    meta = json.loads((snap / "meta.json").read_text())
    sid = result["id"]
    k, d = result["sides"]["keep"], result["sides"]["delegate"]
    # Built before any gh mutation or push, so a body it cannot build (or a body
    # GitHub would reject) is caught before anything is created or pushed.
    body = _pr_body(sid, snap, k, d)
    origin = run_on_source("git", "-C", meta["toplevel"], "remote", "get-url", "origin").strip()
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
    ignored = {"keep": restored_ignored(keep), "delegate": restored_ignored(delegate)}
    for side, clone in (("keep", keep), ("delegate", delegate)):
        commit_all(clone, f"jev fork check {sid}: result")
        refuse_if_ignored_leaked(clone, ignored[side], side)
    for side, clone in (("keep", keep), ("delegate", delegate)):
        run("git", "-C", str(clone), *NO_HOOKS, "push", "-q", url, f"HEAD:refs/heads/replay/{sid}/{side}")
    # The comparison commit is built without checking anything out, so neither
    # clone's files change after the replay.
    run("git", "-C", str(keep), *NO_HOOKS, "fetch", "-q", str(delegate), "HEAD")
    tree = run("git", "-C", str(keep), "rev-parse", "FETCH_HEAD^{tree}").strip()
    compare = run(
        "git",
        "-C",
        str(keep),
        *NO_HOOKS,
        *IDENTITY,
        "commit-tree",
        tree,
        "-p",
        "HEAD",
        "-m",
        f"jev fork check {sid}: comparison",
    ).strip()
    run("git", "-C", str(keep), *NO_HOOKS, "push", "-q", url, f"{compare}:refs/heads/replay/{sid}/compare")
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
