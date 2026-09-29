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


def _resolves_inside(top: Path, link: Path) -> bool:
    """True only for a symlink that is relative and whose target, resolved from
    the link's own directory, stays inside `top`. An absolute link, or one that
    escapes `top`, is never safe to keep as a link in a disposable clone."""
    raw = os.readlink(link)
    if os.path.isabs(raw):
        return False
    target = (link.parent / raw).resolve()
    try:
        target.relative_to(top.resolve())
    except ValueError:
        return False
    return True


def _collect_exclusions(top: Path, rel: str) -> set[str]:
    """Everything under the ignored entry `rel` that must not be copied, found by
    looking only, never following: a nested checkout (a directory holding its own
    `.git`, whose `gitdir:` file can point straight back at the user's real
    repository), a Python virtualenv (a directory holding `pyvenv.cfg`, whose
    absolute paths in `bin/*` shebangs and `.pth`/`direct_url.json` files point at
    the source's own venv), or a symlink that is absolute or escapes `top` (which
    `cp -cRp` would keep as a link into the user's real filesystem, and a naive
    walk would then follow). Returns paths relative to `top`. Nothing here reads
    through a symlink: every check is `lexists`/`is_symlink`, and `os.walk` only
    ever runs on a path already known not to be one."""
    entry = top / rel
    excluded: set[str] = set()

    def unsafe(path: Path, path_rel: str) -> bool:
        """True if `path` itself must be excluded; either way, once this returns
        for a symlink, `path`'s contents (if any) are never looked at."""
        if path.is_symlink():
            if not _resolves_inside(top, path):
                excluded.add(path_rel)
            return True
        if os.path.lexists(path / ".git") or os.path.lexists(path / "pyvenv.cfg"):
            excluded.add(path_rel)
            return True
        return False

    if unsafe(entry, rel) or not entry.is_dir():
        return excluded
    for dirpath, dirnames, filenames in os.walk(entry, followlinks=False):
        base = os.path.relpath(dirpath, top)
        keep = []
        for name in dirnames:
            path_rel = f"{base}/{name}"
            if not unsafe(Path(dirpath) / name, path_rel):
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            unsafe(Path(dirpath) / name, f"{base}/{name}")
    return excluded


def _copy_selective(top: Path, rel: str, target: Path, excluded: set[str]) -> None:
    """Copy `top/rel` into `target`, honoring `excluded` (paths relative to
    `top`, from `_collect_exclusions`): a path in it is skipped outright. A path
    that is an ancestor of an excluded one cannot be copied whole (`cp` cannot
    leave part of a directory out), so it is opened up instead: `mkdir`, keep its
    mode, then recurse into each child with `os.scandir`. Anything else is copied
    in one shot with `copy_tree`, which is what keeps this cheap on a
    copy-on-write filesystem."""
    if rel in excluded:
        return
    source = top / rel
    prefix = f"{rel}/"
    if any(path.startswith(prefix) for path in excluded):
        target.mkdir(parents=True, exist_ok=True)
        shutil.copymode(source, target)
        for child in os.scandir(source):
            _copy_selective(top, f"{rel}/{child.name}", target / child.name, excluded)
    else:
        copy_tree(source, target)


def _reject_stray_checkouts(clone: Path) -> None:
    """A defense-in-depth backstop that runs after every ignored entry has been
    copied: if `_collect_exclusions`/`_copy_selective` ever let a nested checkout
    through despite every earlier safeguard, this is what stops it from reaching
    the caller as an ordinary clone, instead of a `.git` quietly sitting there
    with a `gitdir:` pointer back at the user's real repository."""
    stray = [g for g in clone.rglob(".git") if g != clone / ".git"]
    if stray:
        raise Inconclusive(f"a nested checkout survived pruning at {stray[0].parent}")


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
        # drifted since the snapshot. `git status --porcelain` cannot see that on
        # its own: it drops file content, and it collapses an untracked directory
        # into one line regardless of what ends up nested inside it. But without
        # drift an ignored path can never already exist in the clone (the clone
        # holds only the tracked tree and the tar's non-ignored untracked files),
        # so checking that directly catches what the status text cannot.
        # `--untracked-files=all` is kept as a second check, since it still
        # widens what the comparison can see.
        before = run("git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all")
        skipped: list[str] = []
        for rel in snapshot.ignored_entries(top):
            if os.path.lexists(clone / rel):
                raise Inconclusive(f"{rel} already exists in the clone; ignore rules changed since the snapshot")
            excluded = _collect_exclusions(top, rel)
            _copy_selective(top, rel, clone / rel, excluded)
            skipped.extend(excluded)
        after = run("git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all")
        if after != before:
            raise Inconclusive("ignored files changed since the snapshot")
        _reject_stray_checkouts(clone)
        (dest / "restore.json").write_text(json.dumps({"skipped": sorted(skipped)}, indent=2) + "\n")
    return clone
