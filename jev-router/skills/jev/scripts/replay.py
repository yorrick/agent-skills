"""Replay a snapshot both ways: restore it into clones, run keep and delegate, measure."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import snapshot

IDENTITY = ["-c", "user.name=jev fork check", "-c", "user.email=jev-fork-check@localhost"]

# `restore` reads the same "what counts as ignored" list the hook used to
# fingerprint dependencies; this alias lets a caller reach it through `replay`
# without importing `snapshot` directly.
ignored_entries = snapshot.ignored_entries


class Inconclusive(RuntimeError):
    """The job cannot be replayed faithfully; it is replaced by the next marked one."""


def _command_name(args: tuple[str, ...]) -> str:
    """The part of a git invocation worth naming in an error: the subcommand, not
    the `-C <path>` (and any `-c key=value`) that comes before it. Any other
    program (`cp`) is just named as itself."""
    if not args or args[0] != "git":
        return args[0] if args else ""
    rest = list(args[1:])
    while len(rest) >= 2 and rest[0] in ("-C", "-c"):
        rest = rest[2:]
    return f"git {rest[0]}" if rest else "git"


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{_command_name(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def copy_tree(source: Path, target: Path) -> None:
    """A copy-on-write clone on macOS, so node_modules costs no time or space."""
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = ["-cRp"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
    run("cp", *flags, str(source), str(target))


def _is_special_dir(path: Path) -> bool:
    """A directory that must never end up inside a disposable clone: a nested
    checkout (holds its own `.git`, whose `gitdir:` file can point straight back
    at the user's real repository) or a Python virtualenv (holds `pyvenv.cfg`,
    whose absolute paths in `bin/*` shebangs and `.pth`/`direct_url.json` files
    point at the source's own venv)."""
    return (path / ".git").exists() or (path / "pyvenv.cfg").exists()


def _prune_copy(target: Path, top_rel: str) -> list[str]:
    """After an ignored entry has been copied wholesale, remove anything inside it
    that `_is_special_dir` flags, without descending into it first. `target` may
    itself be such a directory (an ignored entry that is itself a nested checkout
    or a venv), or hold one anywhere underneath. Returns each skipped path
    relative to the snapshot's toplevel."""
    if _is_special_dir(target):
        shutil.rmtree(target)
        return [top_rel]
    skipped: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(target, topdown=True):
        keep = []
        for name in dirnames:
            sub = Path(dirpath) / name
            if _is_special_dir(sub):
                skipped.append(f"{top_rel}/{sub.relative_to(target)}")
                shutil.rmtree(sub)
            else:
                keep.append(name)
        dirnames[:] = keep
    return skipped


def _copy_info_exclude(top: Path, clone: Path) -> None:
    """`.git/info/exclude` is never tracked, so a plain clone never carries it: a
    file the source only ignores there (not through a committed .gitignore) would
    otherwise show up untracked in the clone, and a later `git add -A` would
    publish it. `--git-common-dir` resolves against `top` because it may be
    relative, and in a linked worktree it points at the main checkout's `.git`."""
    common = run("git", "-C", str(top), "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common) if Path(common).is_absolute() else top / common
    source = common_dir / "info" / "exclude"
    if source.exists():
        target = clone / ".git" / "info"
        target.mkdir(parents=True, exist_ok=True)
        (target / "exclude").write_bytes(source.read_bytes())


def restore(snap: Path, dest: Path, *, copy_ignored: bool = True) -> Path:
    meta = json.loads((snap / "meta.json").read_text())
    top, head = Path(meta["toplevel"]), meta["head"]
    if copy_ignored and snapshot.ignored_fingerprint(top) != meta["ignored_fingerprint"]:
        raise Inconclusive("dependencies or .env files changed since the snapshot")
    dest.mkdir(parents=True)
    bare, clone = dest / "origin.git", dest / "repo"
    run("git", "clone", "-q", "--bare", str(top), str(bare))
    # Reason: the bare copy has already fetched everything it needs; dropping
    # "origin" leaves it with no configured path back to the source.
    run("git", "-C", str(bare), "remote", "remove", "origin")
    branch = meta["branch"] if meta["branch"] != "HEAD" else "jev-snapshot"
    if subprocess.run(["git", "-C", str(bare), "cat-file", "-e", head], capture_output=True).returncode != 0:
        run("git", "-C", str(bare), "fetch", "-q", str(top), head)
    # Reason: the branch may have moved on since the snapshot; point it back so the
    # clone checks out exactly the snapshot's commit.
    run("git", "-C", str(bare), "update-ref", f"refs/heads/{branch}", head)
    run("git", "clone", "-q", "--branch", branch, str(bare), str(clone))
    _copy_info_exclude(top, clone)
    diff = snap / "changes.diff"
    if diff.stat().st_size:
        run("git", "-C", str(clone), "apply", "--binary", str(diff))
    try:
        with tarfile.open(snap / "untracked.tar") as tar:
            tar.extractall(clone, filter="data")
    except tarfile.TarError as exc:
        raise Inconclusive(f"untracked.tar could not be extracted: {exc}") from exc
    # The start state as a commit, without touching the index or the working tree,
    # so the judge can diff each result against exactly where the agent began. The
    # index starts as a copy of the clone's own (already checked out from `head`),
    # not an empty one: `git add -A` on an empty index would treat every tracked
    # file that happens to match an ignore pattern (a force-added file) as a new,
    # excluded, untracked path and silently drop it from the tree.
    index_copy = dest / "start.index"
    shutil.copy2(clone / ".git" / "index", index_copy)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_copy)}
    run("git", "-C", str(clone), "add", "-A", env=env)
    tree = run("git", "-C", str(clone), "write-tree", env=env).strip()
    start = run(
        "git", "-C", str(clone), *IDENTITY, "commit-tree", tree, "-p", head, "-m", "jev fork check: start"
    ).strip()
    run("git", "-C", str(clone), "update-ref", "refs/jev/start", start)
    if copy_ignored:
        # Reason: the ignore list is the source's current one, which may have
        # drifted since the snapshot (a path that was tracked then may be
        # untracked and ignored now), and a copy's target may already exist from
        # the checkout or the tar above. Either turns the copy into a silent
        # mutation of the clone rather than an addition, so it must be visible in
        # `git status` or not happen at all; if it changed anything the checkout
        # did not already account for, the replay cannot be trusted.
        before = run("git", "-C", str(clone), "status", "--porcelain")
        skipped: list[str] = []
        for rel in snapshot.ignored_entries(top):
            copy_tree(top / rel, clone / rel)
            skipped.extend(_prune_copy(clone / rel, rel))
        after = run("git", "-C", str(clone), "status", "--porcelain")
        if after != before:
            raise Inconclusive("ignored files changed since the snapshot")
        stray = [g for g in clone.rglob(".git") if g != clone / ".git"]
        if stray:
            raise Inconclusive(f"a nested checkout survived pruning at {stray[0].parent}")
        (dest / "restore.json").write_text(json.dumps({"skipped": skipped}, indent=2) + "\n")
    return clone
