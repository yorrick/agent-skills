"""Replay a snapshot both ways: restore it into clones, run keep and delegate, measure."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import snapshot
import usage
import warmup_hook

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


def run_on_source(*args: str) -> str:
    """A git call that reads the user's real checkout. It runs with
    GIT_OPTIONAL_LOCKS=0 (`snapshot.git_env`), so it never takes
    `.git/index.lock` away from a `git add` the user's own session is running."""
    return run(*args, env=snapshot.git_env())


def copy_tree(source: Path, target: Path) -> None:
    """A copy-on-write clone on macOS, so node_modules costs no time or space."""
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = ["-cRp"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
    run("cp", *flags, str(source), str(target))


def _climbs_out(parent_rel: str, raw: str) -> bool:
    """Read as text, with no lookup on disk: whether a link target is absolute,
    or its `..` steps climb above the top of the tree, counted from
    `parent_rel` (the folder holding the link, relative to that top). A target
    can climb out and come back in by name (`../app/x` from the top of `app`):
    it resolves inside the source, but in a clone with another name it points
    somewhere else."""
    if os.path.isabs(raw):
        return True
    norm = os.path.normpath(os.path.join(parent_rel, raw))
    return norm == ".." or norm.startswith("../")


def _inside(root: Path, path: Path) -> bool:
    """Whether `path`, with every link followed, lands inside `root`. A link
    loop counts as outside: Python 3.12 raises RuntimeError on one."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, RuntimeError, OSError):
        return False
    return True


def _resolves_inside(top: Path, link: Path) -> bool:
    """True only for a symlink that is relative, never climbs above `top` on
    its way, and whose target, resolved from the link's own directory, stays
    inside `top`. Any other link is never safe to keep in a disposable clone."""
    raw = os.readlink(link)
    if _climbs_out(os.path.relpath(link.parent, top), raw):
        return False
    return _inside(top, link.parent / raw)


def _refuse_escaping_links(clone: Path, env: dict) -> None:
    """Every symlink in the start state (checked out, applied from the diff or
    extracted from the tar: the start index `env` names lists them all) must
    point inside the clone. A committed `data -> /Users/me/work/app/data`
    would otherwise let a replay write straight through it into the real
    checkout, and the leak scan would never see the real path spelled out.
    Each target is read from its blob, as git recorded it, and checked both as
    text and as resolved on disk."""
    listing = run("git", "-C", str(clone), "ls-files", "-s", "-z", env=env)
    links = [(e.split("\t", 1)[1], e.split()[1]) for e in listing.split("\0") if e.startswith("120000 ")]
    if not links:
        return
    blobs = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "--batch"],
        input="".join(f"{oid}\n" for _, oid in links).encode(),
        capture_output=True,
    )
    if blobs.returncode != 0:
        raise RuntimeError(f"git cat-file failed: {blobs.stderr.decode(errors='replace').strip()[:300]}")
    out, pos = blobs.stdout, 0
    for path, _ in links:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        target = os.fsdecode(out[header_end + 1 : header_end + 1 + size])
        pos = header_end + 2 + size
        if _climbs_out(os.path.dirname(path) or ".", target) or not _inside(clone, clone / path):
            raise Inconclusive(f"the symlink {path} points outside the clone ({target})")


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
    leave part of a directory out), so it is opened up instead: `mkdir`, recurse
    into each child with `os.scandir`, then keep its mode. Anything else is
    copied in one shot with `copy_tree`, which is what keeps this cheap on a
    copy-on-write filesystem."""
    if rel in excluded:
        return
    source = top / rel
    prefix = f"{rel}/"
    # Reason: a symlink is copied as the link it is, never opened up, so nothing
    # is ever read (or written) through it.
    if not source.is_symlink() and any(path.startswith(prefix) for path in excluded):
        target.mkdir(parents=True, exist_ok=True)
        for child in os.scandir(source):
            _copy_selective(top, f"{rel}/{child.name}", target / child.name, excluded)
        # Reason: after the children, never before: a read-only folder's mode
        # would refuse them.
        shutil.copymode(source, target)
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
    common = run_on_source("git", "-C", str(top), "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common) if Path(common).is_absolute() else top / common
    source = common_dir / "info" / "exclude"
    if source.exists():
        target = clone / ".git" / "info"
        target.mkdir(parents=True, exist_ok=True)
        (target / "exclude").write_bytes(source.read_bytes())


def restore(snap: Path, dest: Path, *, copy_ignored: bool = True) -> Path:
    """Rebuild the snapshot's working copy in `dest/repo`. With `copy_ignored`,
    also copy in the ignored entries the snapshot captured (paths recorded in
    meta.json), as they are now in the real checkout, and record what happened
    to them in `dest/restore.json`: `ignored` (copied), `skipped` (nested
    checkouts, virtualenvs and escaping links left out), `missing` (captured but
    gone since), `not_captured` (ignored now but new since the capture, never
    copied: a secret added to info/exclude, a cache, the real turn's output) and
    `changed_since_capture` (captured, but modified after the capture, never
    copied)."""
    meta = json.loads((snap / "meta.json").read_text())
    top, head = Path(meta["toplevel"]), meta["head"]
    # Reason: a job captured in a worktree the user has since removed can never
    # be restored; saying so makes it inconclusive instead of a crash that would
    # hold one of the report's slots forever.
    if not top.is_dir():
        raise Inconclusive(f"the checkout {top} no longer exists")
    captured: list[str] = []
    if copy_ignored:
        if "ignored_entries" not in meta:
            raise Inconclusive(
                "the snapshot records no list of ignored entries (it predates this runner), so it cannot be replayed"
            )
        captured = meta["ignored_entries"]
        # Reason (accepted in Ruling F11): the fingerprint covers only the
        # captured entries, since only they are ever copied; an ignored file
        # created after the capture cannot make the job inconclusive.
        if snapshot.ignored_fingerprint(top, entries=captured) != meta["ignored_fingerprint"]:
            raise Inconclusive("dependencies or .env files changed since the snapshot")
    dest.mkdir(parents=True)
    bare, clone = dest / "origin.git", dest / "repo"
    # Reason: a clone from a local path hard-links the object files, so a chmod
    # or an in-place write inside the copy would reach the real `.git/objects`;
    # `--no-hardlinks` copies them. A source that borrows objects (made with
    # `--shared` or `--reference`) would also pass its `objects/info/alternates`
    # on, so the copy kept reading the lender's store; `--dissociate` copies the
    # borrowed objects in and drops that file (git accepts it without
    # `--reference` for exactly this case). The fetch below goes through the pack
    # protocol and never links, and the clone of the bare copy only links
    # scratch files to scratch files.
    run_on_source("git", "clone", "-q", "--bare", "--no-hardlinks", "--dissociate", str(top), str(bare))
    # Reason: the bare copy has already fetched everything it needs; dropping
    # "origin" leaves it with no configured path back to the source.
    run("git", "-C", str(bare), "remote", "remove", "origin")
    branch = meta["branch"] if meta["branch"] != "HEAD" else "jev-snapshot"
    if subprocess.run(["git", "-C", str(bare), "cat-file", "-e", head], capture_output=True).returncode != 0:
        run_on_source("git", "-C", str(bare), "fetch", "-q", str(top), head)
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
    _refuse_escaping_links(clone, env)
    run("git", "-C", str(clone), "update-ref", "refs/jev/start", start)
    if copy_ignored:
        # Reason: without drift an ignored path can never already exist in the
        # clone (the clone holds only the tracked tree and the tar's non-ignored
        # untracked files), and `git status --porcelain` cannot see such drift on
        # its own: it drops file content, and it collapses an untracked directory
        # into one line regardless of what ends up nested inside it. The direct
        # check catches what the status text cannot; `--untracked-files=all` is
        # kept as a second check, since it still widens what the comparison sees.
        before = run("git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all")
        skipped: list[str] = []
        ignored: list[str] = []
        missing: list[str] = []
        changed: list[str] = []
        # Reason (Ruling F23b): `created` is kept to the microsecond, so it is
        # the cutoff itself. (An older snapshot's `created`, kept to the second,
        # would make this cutoff up to a second early, which only leaves more out.)
        cutoff = datetime.fromisoformat(meta["created"]).timestamp()
        for rel in captured:
            if not os.path.lexists(top / rel):
                missing.append(rel)
                continue
            # Reason (Ruling F19): an entry the real turn (or the user) changed
            # after the capture is not the state the job started from; it is
            # left out and listed rather than making the job inconclusive.
            # Dependency folders are exempt: the fingerprint watches them.
            if not _in_dependency_folder(rel) and _touched_since(top / rel, cutoff):
                changed.append(rel)
                continue
            if os.path.lexists(clone / rel):
                raise Inconclusive(f"{rel} already exists in the clone")
            # Reason: a folder on the way could be a link out of the clone, and
            # the copy would write through it.
            if not _inside(clone, (clone / rel).parent):
                raise Inconclusive(f"{rel} would be copied through a link out of the clone")
            excluded = _collect_exclusions(top, rel)
            _copy_selective(top, rel, clone / rel, excluded)
            skipped.extend(excluded)
            ignored.append(rel)
        not_captured = sorted(set(snapshot.ignored_entries(top)) - set(captured))
        after = run("git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all")
        if after != before:
            raise Inconclusive("ignored files changed since the snapshot")
        _reject_stray_checkouts(clone)
        # Reason: "ignored" (the top-level entries actually copied in, such as
        # ".env") lets a later publish step refuse to push one of them even if
        # the clone's own ignore rules later change or are force-added around,
        # since the replay runs with bypass permissions and can rewrite them.
        # "ignored_blobs" records their content now, before the replay runs, so
        # the content check still knows a `.env` the replay moved away (Ruling
        # F10): every restored ignored regular file outside dependency folders,
        # up to CONTENT_MAX_BYTES, however small (Ruling F32), except the
        # trivial contents that are never secrets (`_TRIVIAL`, Rulings F38 and
        # F40; only now is the content at hand). The history exemption
        # (`_secrets`) applies where the record is used.
        # "ignored_blob_sizes" lets the checks look for those contents anywhere
        # by hashing only the files of a matching size.
        contents = _restored_files(clone, ignored)
        hashed = zip(_hash_files(clone, contents), contents, strict=True)
        blobs = {oid: path for (oid, can_be_secret), path in hashed if can_be_secret}
        record = {
            "skipped": sorted(skipped),
            "ignored": sorted(ignored),
            "missing": missing,
            "not_captured": not_captured,
            "changed_since_capture": changed,
            "ignored_blobs": blobs,
            "ignored_blob_sizes": {oid: os.lstat(clone / path).st_size for oid, path in blobs.items()},
        }
        (dest / "restore.json").write_text(json.dumps(record, indent=2) + "\n")
    return clone


def refuse_if_unusable(result: dict) -> None:
    """Neither an inconclusive replay nor a side with no priced cost is usable:
    the first has nothing worth comparing, and the second would either crash
    building a report or post cost figures that are not real. Shared by
    publish and the judge, so neither ever acts on a result the other would
    refuse."""
    sid = result.get("id", "?")
    if result.get("inconclusive"):
        raise RuntimeError(f"{sid} is inconclusive ({result.get('reason', '')}); nothing to publish or judge")
    sides = result.get("sides") or {}
    for side in ("keep", "delegate"):
        if side not in sides or "cost" not in sides[side]:
            raise RuntimeError(f"{sid} has no priced cost for {side}; nothing to publish or judge")


def _restore_record(clone: Path, key: str) -> Any:
    """One field of the `restore.json` next to this clone. Raises rather than
    assuming "nothing was restored" when the record or the field is missing:
    an empty answer here would quietly disable a guard instead of refusing."""
    info = clone.parent / "restore.json"
    if not info.exists():
        raise RuntimeError(f"{info} is missing; cannot tell what restore copied in, refusing to use {clone}")
    data = json.loads(info.read_text())
    if key not in data:
        raise RuntimeError(f'{info} has no "{key}" record; refusing to use {clone}')
    return data[key]


def restored_ignored(clone: Path) -> list[str]:
    """The top-level entries `restore` copied into this clone, which must never
    end up published or shown to the judge, no matter what the clone's own
    ignore rules say by the time it is checked."""
    return list(_restore_record(clone, "ignored"))


def restored_blobs(clone: Path) -> dict[str, str]:
    """Blob id to path of the restored ignored files, as `restore` hashed them
    before the replay ran."""
    return dict(_restore_record(clone, "ignored_blobs"))


def _matches_ignored(path: str, ignored: list[str]) -> str | None:
    for rel in ignored:
        if path == rel or path.startswith(rel + "/"):
            return rel
    return None


def _status_paths(output: str) -> list[str]:
    """The paths named by `git status --porcelain -z --no-renames`: one per
    NUL-separated entry (two status letters, a space, then the path).
    `--no-renames` is what keeps every entry to exactly one path: with rename
    or copy detection on, an R or C entry carries a second, unprefixed field
    (the old path), and a parser that does not account for it drifts by one
    field for every entry that follows, silently losing whatever comes after
    (a later `.env` included)."""
    return [e[3:] for e in output.split("\0") if e]


def _tree(clone: Path, ref: str) -> list[tuple[str, str, str]]:
    """(type, object id, path) of every entry in `ref`'s tree, read with `-z`."""
    out = run("git", "-C", str(clone), "ls-tree", "-r", "-z", ref)
    entries = []
    for line in out.split("\0"):
        if line:
            info, path = line.split("\t", 1)
            _, kind, oid = info.split()
            entries.append((kind, oid, path))
    return entries


# Restored ignored files of up to this size are secrets (Ruling F32), however
# small: a credential is small, while a bigger file is a build output or a cache,
# and the cap keeps a restored `target/` or `.next/cache` from being read in full.
CONTENT_MAX_BYTES = 1 << 20


def _in_dependency_folder(rel: str) -> bool:
    return any(part in snapshot.DEPENDENCY_DIRS for part in Path(rel).parts)


def _touched_since(path: Path, cutoff: float) -> bool:
    """Whether `path`, or anything under it, was modified at or after `cutoff`
    (a timestamp). Looks without following links: lstat, and os.walk without
    followlinks. An entry that vanishes while it is looked at is being changed
    right now, so it counts as touched."""

    def touched(entry: str | Path) -> bool:
        try:
            return os.lstat(entry).st_mtime >= cutoff
        except FileNotFoundError:
            return True

    if touched(path):
        return True
    if path.is_symlink() or not path.is_dir():
        return False
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            if touched(os.path.join(dirpath, name)):
                return True
    return False


def _never_restored(folder: Path) -> bool:
    """A folder restore never copies, as `_collect_exclusions` decides: a nested
    checkout or a virtualenv. Whatever sits there later, such as the `.venv` the
    replay's `uv sync` built where the user's own was captured, is the replay's
    own, not restored content."""
    # Known limit (N4, deliberate smuggling, Ruling F35): a `.git` or `pyvenv.cfg` planted in a restored ignored
    # folder hides that folder's edited files from the check-time hashing, for publish as for the judge.
    return os.path.lexists(folder / ".git") or os.path.lexists(folder / "pyvenv.cfg")


def _restored_files(clone: Path, ignored: list[str]) -> list[str]:
    """The regular files now under the restored ignored entries, relative to the
    clone, outside dependency folders and folders restore never copies, of at
    most CONTENT_MAX_BYTES. Nothing here follows a symlink."""
    found: list[str] = []

    def consider(rel: str) -> None:
        st = os.lstat(clone / rel)
        if stat.S_ISREG(st.st_mode) and st.st_size <= CONTENT_MAX_BYTES:
            found.append(rel)

    for rel in ignored:
        path = clone / rel
        if not os.path.lexists(path) or _in_dependency_folder(rel):
            continue
        if path.is_symlink() or not path.is_dir():
            consider(rel)
            continue
        if _never_restored(path):
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in snapshot.DEPENDENCY_DIRS and not _never_restored(Path(dirpath) / name)
            ]
            base = os.path.relpath(dirpath, clone)
            for name in filenames:
                consider(f"{base}/{name}")
    return found


def _regular_size(path: Path) -> int | None:
    """The size of `path` if it is a regular file (lstat: a link is not
    followed), else None, as for a path that no longer exists."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st.st_size if stat.S_ISREG(st.st_mode) else None


def _secrets(clone: Path, ignored: list[str]) -> dict[str, tuple[str, int]]:
    """Blob id to (path, size) of every content that must never reach GitHub or
    the judge under any name, shared by publish, the judge's refusal and its
    copies (Ruling F32): the ids `restore` recorded before the replay ran (so a
    moved `.env` still counts, Ruling F10), and the restored files as they are
    now (so one the replay edited, then copied, counts too).

    Two kinds of content are never secrets. The trivial contents in
    `_TRIVIAL` (Rulings F38, F40): the empty file, and `{}`, `[]` or `null`
    once ASCII whitespace is stripped, which caches write by the hundred;
    restore leaves them out of its record, and they are dropped here from the
    files as they are now. And any blob reachable from `refs/jev/start^`
    (Rulings F11, F13): the user's committed history already holds it, and the
    push carries that history anyway, so a `.env` made from a tracked
    `.env.example` must not refuse every job. Only the snapshot HEAD's history
    is exempt, never the start tree's untracked files from the tar.

    With nothing to protect, this returns before any file is hashed (one that
    cannot be read would fail the check) or any history is walked."""
    recorded = restored_blobs(clone)
    now = _restored_files(clone, ignored)
    if not recorded and not now:
        return {}
    sizes = _restore_record(clone, "ignored_blob_sizes")
    secrets = {oid: (path, int(sizes[oid])) for oid, path in recorded.items()}
    for (oid, can_be_secret), path in zip(_hash_files(clone, now), now, strict=True):
        if can_be_secret:
            secrets.setdefault(oid, (path, os.lstat(clone / path).st_size))
    if secrets:
        for oid, _ in _object_ids(clone, "refs/jev/start^"):
            secrets.pop(oid, None)
    return secrets


def _is_venv(path: Path) -> bool:
    """Whether `path` is a real folder (not a link) holding a regular file
    `pyvenv.cfg` at its root: a Python virtualenv."""
    try:
        return not path.is_symlink() and stat.S_ISREG(os.lstat(path / "pyvenv.cfg").st_mode)
    except OSError:
        return False


def _around_venvs(clone: Path, rel: str) -> tuple[set[str], list[str]]:
    """What to leave out of the ignored entry `rel`, and the virtualenvs inside
    it to keep: `rel` itself and none, or, when a virtualenv sits inside it
    (`.tox/py312`), every path under `rel` that is neither a virtualenv nor on
    the way to one, and those virtualenvs. Looks without following links."""
    path = clone / rel
    venvs: list[str] = []
    if not path.is_symlink() and path.is_dir():
        for dirpath, dirnames, _ in os.walk(path):
            base = os.path.relpath(dirpath, clone)
            found = [d for d in dirnames if _is_venv(Path(dirpath) / d)]
            venvs += [f"{base}/{d}" for d in found]
            dirnames[:] = [d for d in dirnames if d not in found]
    if not venvs:
        return {rel}, []
    left_out: set[str] = set()

    def open_up(folder: str) -> None:
        for child in os.scandir(clone / folder):
            sub = f"{folder}/{child.name}"
            if any(v.startswith(f"{sub}/") for v in venvs):
                open_up(sub)
            elif sub not in venvs:
                left_out.add(sub)

    open_up(rel)
    return left_out, venvs


def _judge_left_out(clone: Path) -> tuple[set[str], list[str]]:
    """What the clone ignores now, apart from what the judge needs to run the
    tests, and the ignored paths kept for that: dependency folders, and
    virtualenvs (a folder holding `pyvenv.cfg` at its root) with everything in
    them. A virtualenv here is the one the replay built in the clone: restore
    never copies the user's own (Ruling T9d). The clone's own root never counts
    as one."""
    left_out: set[str] = set()
    kept: list[str] = []
    for rel in snapshot.ignored_entries(clone):
        parts = Path(rel).parts
        # Residual (F28 class, accepted in F33): a replay can plant `node_modules` or `pyvenv.cfg` to keep a folder.
        if _in_dependency_folder(rel) or any(_is_venv(clone.joinpath(*parts[:i])) for i in range(1, len(parts) + 1)):
            kept.append(rel)
            continue
        out, venvs = _around_venvs(clone, rel)
        left_out |= out
        kept += venvs
    return left_out, kept


_MAX_HOPS = 40


def _leads_out(copy: Path, link: Path) -> bool:
    """Whether following `link` the way the system does leaves `copy` by a
    step no kept link takes (Rulings F37, F41). The target is taken one
    component at a time: `..` climbs from wherever the path has got to, so
    after a link it climbs from that link's target, which reading the target
    as text misses (`up -> ../../..` with `esc -> up/../../home/.ssh/id_rsa`
    reaches a key outside). A step out taken by a link that sits in a
    dependency folder or a virtualenv is the accepted kept-link class, so a
    convenience `python -> .venv/bin/python` stays; naming a venv on the way
    (`.venv/../up/..`) takes no such step. A loop leads nowhere. Only lstat
    and readlink are used; nothing is opened."""
    first = os.readlink(link)
    if os.path.isabs(first):
        return True
    parts = list(link.relative_to(copy).parts[:-1])
    # A stack, next component last; each carries whether a kept link's target holds it.
    pending = [(name, False) for name in reversed(first.split("/"))]
    hops = 0
    while pending:
        name, kept = pending.pop()
        if name in ("", "."):
            continue
        if name == "..":
            if not parts:
                return not kept
            parts.pop()
            continue
        parts.append(name)
        here = copy.joinpath(*parts)
        if not here.is_symlink():
            continue
        hops += 1
        if hops > _MAX_HOPS:
            return False
        target = os.readlink(here)
        parts.pop()
        in_kept = any(p in snapshot.DEPENDENCY_DIRS for p in parts) or any(
            _is_venv(copy.joinpath(*parts[:i])) for i in range(1, len(parts) + 1)
        )
        if os.path.isabs(target):
            return not in_kept
        pending.extend((step, in_kept) for step in reversed(target.split("/")))
    return False


def _drop_links_out(copy: Path) -> None:
    """Deal with every symlink in `copy` that leads out of it (`_leads_out`,
    Rulings F29, F37, F41): a replay can leave `key -> ~/.ssh/id_rsa`, or two
    links that each look inside by their text and chain out, which the judge
    would follow. Links inside dependency folders and virtualenvs stay, since
    tests need them (a venv's `python` is an absolute link to the
    interpreter), and those folders are not walked.

    Outside `.git`, every path here is the replay's result, tracked or
    untracked and not ignored, since the copy leaves ignored paths out. Such a
    link is never removed silently: the judge would measure a result that is
    not the replay's, so judging is refused. One inside `.git` is removed.
    The walk looks with lstat and never enters a linked folder, and only the
    link itself is ever removed."""
    for dirpath, dirnames, filenames in os.walk(copy):
        here = Path(dirpath)
        walk_on = []
        for name in dirnames + filenames:
            path = here / name
            if path.is_symlink():
                if not _leads_out(copy, path):
                    continue
                rel = path.relative_to(copy)
                if rel.parts[0] != ".git":
                    raise RuntimeError(f"the result holds a link outside the repository: {rel}; refusing")
                path.unlink()
            elif name in dirnames and name not in snapshot.DEPENDENCY_DIRS and not _is_venv(path):
                walk_on.append(name)
        dirnames[:] = walk_on


def _url_path(path: str) -> str:
    """`path` as a `file://` URL holds it: percent-encoded, `/` kept."""
    return urllib.parse.quote(path, safe="/")


def _moves(clone: Path, target: Path) -> dict[bytes, bytes]:
    """Each spelling of `clone`'s path in files, and the same spelling of
    `target`'s. The path as given and fully resolved (`/var` is `/private/var`
    on macOS; uv writes the resolved one), each also percent-encoded, as the
    `file://` URL in an editable install's `direct_url.json` holds it when the
    path has a space or a non-ASCII character (Ruling F42). The target is
    resolved first (Ruling F43): uv records absolute paths, so a copy given as
    a relative path would otherwise get `file://j/A` or a `.pth` pointing away
    from it."""
    raws = (str(clone), str(clone.resolve()))
    destination = str(target.resolve())
    moves = {raw.encode(): destination.encode() for raw in raws}
    for raw in raws:
        moves.setdefault(_url_path(raw).encode(), _url_path(destination).encode())
    return moves


def clone_spellings(clone: Path) -> list[bytes]:
    """Every spelling of the clone's path in files (`_moves`), longest first."""
    return sorted(_moves(clone, clone), key=len, reverse=True)


def _relocate_venvs(clone: Path, target: Path, venvs: list[str]) -> None:
    """Point each virtualenv in the copy at the copy instead of the clone
    (Ruling F36). uv writes the clone's absolute path into the editable
    `_editable_impl_*.pth`, `direct_url.json`, every `bin/*` entry script and
    shebang and the `activate*` scripts, so the copy's `python` would import
    the clone's code, which can read the clone's restored `.env`. In text files
    (no NUL byte) of up to 1 MB, each spelling of the clone's path followed by a
    path boundary becomes the same spelling of the copy's path, raw or
    percent-encoded (`_moves`), byte for byte. Binary files are left alone:
    `judge` scans the venvs afterwards and refuses on any spelling left."""
    moves = _moves(clone, target)
    pattern = re.compile(
        b"(?:" + b"|".join(re.escape(s) for s in sorted(moves, key=len, reverse=True)) + rb")(?![\w.-])"
    )
    for venv in venvs:
        for dirpath, _, filenames in os.walk(target / venv):
            for name in filenames:
                path = Path(dirpath) / name
                size = _regular_size(path)
                if size is None or size > CONTENT_MAX_BYTES:
                    continue
                data = path.read_bytes()
                if b"\0" in data or not pattern.search(data):
                    continue
                mode = os.lstat(path).st_mode
                path.chmod(mode | stat.S_IWUSR)
                path.write_bytes(pattern.sub(lambda match: moves[match.group(0)], data))
                path.chmod(stat.S_IMODE(mode))


def _fresh_git(clone: Path, target: Path) -> None:
    """Make `target` a repository holding only HEAD's history and
    `refs/jev/start`, fetched from `clone` (Ruling F33), never a copy of the
    clone's `.git`: what a replay left in there (`cp .env .git/notes.txt`, a
    stash, a staged blob, a side branch, tags, reflogs) stays behind.
    `--no-local` sends only reachable objects, as a fetch over the network
    would. HEAD is the clone's own: the same branch, or the same commit when
    the clone's HEAD is detached. The origin remote, the reflogs and
    FETCH_HEAD, which all name the clone's folder, are removed."""
    branch = subprocess.run(
        ["git", "-C", str(clone), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    head = run("git", "-C", str(clone), "rev-parse", "HEAD").strip()
    which = ["--branch", branch.removeprefix("refs/heads/")] if branch else []
    run(
        "git",
        "clone",
        "-q",
        "--no-checkout",
        "--no-local",
        "--single-branch",
        "--no-tags",
        *which,
        str(clone),
        str(target),
    )
    run("git", "-C", str(target), "fetch", "-q", "--no-tags", str(clone), "refs/jev/start:refs/jev/start")
    run("git", "-C", str(target), "remote", "remove", "origin")
    if not branch:
        # Reason: for a detached HEAD, clone puts the copy on a branch that
        # points at the same commit when there is one.
        guessed = subprocess.run(
            ["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        run("git", "-C", str(target), "update-ref", "--no-deref", "HEAD", head)
        if guessed:
            run("git", "-C", str(target), "update-ref", "-d", guessed)
    shutil.rmtree(target / ".git" / "logs", ignore_errors=True)
    (target / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    copied = subprocess.run(
        ["git", "-C", str(target), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if copied != branch or run("git", "-C", str(target), "rev-parse", "HEAD").strip() != head:
        raise RuntimeError(f"the judge's copy of {clone} did not get the clone's HEAD ({branch or head})")


def _ignore_kept(target: Path, kept: list[str]) -> None:
    """List in the copy's `info/exclude` the ignored folders it keeps
    (dependency folders, virtualenvs), anchored and with the pattern
    characters escaped. The clone may ignore them only through its own
    `info/exclude`, which the fresh `.git` does not have; the judge's `git add
    -A` would then show `node_modules` as the replay's work. A name holding a
    newline cannot be written as a pattern and is skipped."""
    lines = []
    for rel in sorted(kept):
        if "\n" not in rel:
            escaped = re.sub(r"([\\*?\[])", r"\\\1", rel)
            lines.append("/" + re.sub(r" $", r"\\ ", escaped) + "\n")
    exclude = target / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("".join(lines))


def _remove_venv_caches(target: Path, caches: list[str]) -> None:
    """Remove the virtualenvs' `__pycache__` folders from the judge's copy:
    every `.pyc` in them names the clone in a binary file, which the scan would
    refuse, and Python rebuilds them (Ruling F39). The venvs are copied whole
    first, in one `cp` each; leaving these out while copying cost one `cp` per
    entry (Ruling F41). Only a real folder at exactly `target/<rel>` is
    removed, never one reached through a link, so nothing outside the copy is
    ever touched."""
    root = target.resolve()
    for rel in caches:
        path = target / rel
        if not path.is_symlink() and path.is_dir() and path.resolve() == root.joinpath(rel):
            shutil.rmtree(path)


def copy_for_judge(clone: Path, target: Path) -> list[str]:
    """Copy `clone` to `target` with only what the judge needs (Rulings F23a,
    F24, F33): tracked files, untracked files the clone does not ignore,
    dependency folders and virtualenvs, so tests can run, and a fresh `.git`
    (`_fresh_git`) whose index is built from HEAD, so status shows the replay's
    changes. Each virtualenv is pointed at the copy (`_relocate_venvs`).
    Returns the virtualenvs, relative to the copy. Left out:

    - the clone's `.git`, and any nested `.git` the replay made (a repository
      of its own, or a gitfile pointing back at the clone's): only the working
      files around it are copied;
    - every path the clone itself ignores now, outside dependency folders and
      virtualenvs (`.env`, `.env.local`, caches), whatever put it there;
    - every other file whose content is a secret (`_secrets`), whatever its
      path (`.env` copied under another name, or into `node_modules`). Only
      files of a secret's size are hashed.

    A link out of the copy in the replay's result refuses judging
    (`_drop_links_out`). Files are left out while copying, and nothing is
    read through a symlink. The one thing deleted afterwards is each
    virtualenv's `__pycache__` folders (`_remove_venv_caches`)."""
    left_out, kept = _judge_left_out(clone)
    left_out.add(".git")
    secrets = _secrets(clone, restored_ignored(clone))
    sizes = {size for _, size in secrets.values()}
    candidates: list[str] = []
    venvs: list[str] = []
    caches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(clone):
        base = os.path.relpath(dirpath, clone)
        prefix = "" if base == "." else f"{base}/"
        # Known limit (N3): the gitfile of an initialized submodule goes too, so edits inside one are invisible to the
        # judge; restore never initializes submodules.
        left_out.update(f"{prefix}{name}" for name in dirnames + filenames if name == ".git")
        in_venv = any(prefix.startswith(f"{v}/") for v in venvs)
        for name in dirnames:
            if in_venv and name == "__pycache__":
                caches.append(f"{prefix}{name}")
            elif not in_venv and _is_venv(Path(dirpath) / name) and f"{prefix}{name}" not in left_out:
                venvs.append(f"{prefix}{name}")
        dirnames[:] = [d for d in dirnames if f"{prefix}{d}" not in left_out and f"{prefix}{d}" not in caches]
        for name in filenames:
            rel = f"{prefix}{name}"
            if sizes and rel not in left_out and _regular_size(clone / rel) in sizes:
                candidates.append(rel)
    left_out.update(rel for rel, oid in zip(candidates, _blob_ids(clone, candidates), strict=True) if oid in secrets)
    _fresh_git(clone, target)
    _copy_selective(clone.parent, clone.name, target, {f"{clone.name}/{p}" for p in left_out})
    _remove_venv_caches(target, caches)
    _ignore_kept(target, kept)
    run("git", "-C", str(target), "read-tree", "HEAD")
    run("git", "-C", str(target), "update-index", "-q", "--refresh")
    _drop_links_out(target)
    _relocate_venvs(clone, target, venvs)
    return venvs


# The contents that are never secrets because of what they hold (Rulings F38,
# F40), once ASCII whitespace is stripped: caches write them by the hundred, and
# a restored `.pytest_cache` file holding `{}` would otherwise match every `{}`
# in the repository, `node_modules` included. The empty file is exempt too.
# Nothing else is exempt by its characters: a raw binary key or a passphrase
# written only in non-ASCII characters is a secret like any other.
_TRIVIAL = {b"{}", b"[]", b"null"}


def _hash_files(clone: Path, paths: list[str]) -> list[tuple[str, bool]]:
    """The git blob id of each of `paths` (relative to the clone), and whether
    that content can be a secret at all (neither empty nor `_TRIVIAL`).
    Computed here from the bytes on disk (Ruling F27): `git hash-object
    --stdin-paths` reads one path per line, so a name holding a newline would
    slip past it. The hash is the clone's own object format (sha1, or sha256
    in a repository made with it), and each file is opened without following a
    symlink."""
    if not paths:
        return []
    algorithm = run("git", "-C", str(clone), "rev-parse", "--show-object-format").strip()
    hashed = []
    for rel in paths:
        try:
            fd = os.open(clone / rel, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise RuntimeError(f"cannot read {rel} to compare it with the restored secrets: {exc}") from exc
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        oid = hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()
        hashed.append((oid, data != b"" and data.strip() not in _TRIVIAL))
    return hashed


def _blob_ids(clone: Path, paths: list[str]) -> list[str]:
    """The git blob id of each of `paths` (`_hash_files`)."""
    return [oid for oid, _ in _hash_files(clone, paths)]


def _index(clone: Path) -> list[tuple[str, str]]:
    """(object id, path) of every entry in the clone's index, every stage
    included, read with `-z`."""
    out = run("git", "-C", str(clone), "ls-files", "-s", "-z")
    entries = []
    for line in out.split("\0"):
        if line:
            info, path = line.split("\t", 1)
            entries.append((info.split()[1], path))
    return entries


def _object_ids(clone: Path, *revs: str) -> list[tuple[str, str]]:
    """(object id, path or "(no path)") of every object `git rev-list --objects`
    lists for `revs`."""
    out = run("git", "-C", str(clone), "rev-list", "--objects", *revs)
    listed = []
    for line in out.splitlines():
        oid, _, path = line.partition(" ")
        listed.append((oid, path or "(no path)"))
    return listed


def _refuse_if_content_leaked(
    clone: Path, ignored: list[str], tree: list[tuple[str, str, str]], status: list[str], label: str
) -> None:
    """Refuses if a secret (`_secrets`: a restored ignored file's exact content)
    shows up under another path: in HEAD's tree, in any object the replay's
    commits introduced, in the index, or in a working-tree file `git status`
    lists. A replay that copies or moves `.env` to `config.txt` would otherwise
    publish the secret, or show it to the judge, under a name no path check
    knows. Only working-tree files of a secret's size are hashed."""
    secrets = _secrets(clone, ignored)
    if not secrets:
        return
    sizes = {size for _, size in secrets.values()}
    candidates = [(oid, path) for kind, oid, path in tree if kind == "blob"]
    # Reason (Ruling F17): from the snapshot HEAD, not from refs/jev/start. The
    # start tree holds the tar's untracked files, so `start..HEAD` would leave
    # out a blob matching one of them that the replay committed under another
    # name and deleted later.
    candidates += _object_ids(clone, "refs/jev/start^..HEAD")
    # Reason (Ruling F26): a copy staged and then deleted or edited in the
    # working tree is still in the index (`git show :config.txt`). The ids come
    # straight from the index, whatever the working file holds now.
    candidates += _index(clone)
    worktree = [p for p in status if _regular_size(clone / p) in sizes]
    candidates += list(zip(_blob_ids(clone, worktree), worktree, strict=True))
    for oid, path in candidates:
        if oid in secrets:
            raise RuntimeError(
                f"{path} in the {label} clone holds the content of the restored ignored file {secrets[oid][0]}; "
                "refusing"
            )


def refuse_if_ignored_leaked(clone: Path, ignored: list[str], label: str) -> None:
    """Refuses if any of `ignored` (or anything under it) is committed at HEAD, was
    ever touched between `refs/jev/start` (where the replay began) and HEAD, or
    merely sits in the working tree right now: a session with bypass permissions
    can drop a `.gitignore` line or force-add a path, so the clone's own current
    ignore rules cannot be trusted; only what `restore` actually copied in can.
    The working-tree check is what catches this before anything is ever
    committed, which matters to the judge: it never commits, so an un-ignored
    `.env` would otherwise show nowhere else. Then the same places, and the
    index, are checked for a restored ignored file's content under another
    name (`_refuse_if_content_leaked`).

    Every listing is read with `-z`: without it, git quotes a path that
    holds a non-ASCII byte, a `"`, a backslash or a control character, so the
    quoted form would never equal the plain name recorded in `restore.json`
    and the check would silently miss it."""
    if not ignored:
        return
    tree = _tree(clone, "HEAD")
    # --diff-merges=m: a path introduced only by how a merge resolved a
    # conflict is still listed, not skipped as merges normally are. The range
    # starts at the snapshot HEAD, like the content check's (Ruling F17).
    history_out = run(
        "git", "-C", str(clone), "log", "--name-only", "-z", "--format=", "--diff-merges=m", "refs/jev/start^..HEAD"
    )
    history = [p for p in history_out.split("\0") if p]
    status_out = run("git", "-C", str(clone), "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    status = _status_paths(status_out)
    for path in [p for _, _, p in tree] + history + status:
        hit = _matches_ignored(path, ignored)
        if hit:
            raise RuntimeError(f"{hit} would be published or shown from the {label} clone (found as {path}); refusing")
    _refuse_if_content_leaked(clone, ignored, tree, status, label)


DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
# Reason: a prompt-cache entry ends with the exact request that wrote it, so the
# runner launches the warm-up exactly like the job: the job's own message and
# environment, with the same flags. That makes the two invocations identical,
# not the two API requests (startup-hook output, regenerated attachments or
# other runtime state can still differ); such a difference can only leave an
# attempt cold, which the per-attempt warmth check turns into an inconclusive
# result, never a wrong measurement. These settings go to every call, warm-up
# and job alike, and their one hook (warmup_hook.py) acts only when WARMUP_VAR
# is "1": it denies the warm-up's first tool and stops it. The hook runs by
# absolute path with this interpreter, never through the user's shell or PATH.
# Claude Code lets a tool run when its hook fails (cannot start, crashes, times
# out, exits non-zero other than 2) unless the hook says `"onFailure": "block"`; in the
# job the hook exits 0, so that never blocks there. Built once, so the two argv
# are byte-identical.
WARMUP_VAR = "JEV_FORK_CHECK_WARMUP"
WARMUP_HOOK = Path(__file__).resolve().with_name("warmup_hook.py")
SETTINGS = json.dumps(
    {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(WARMUP_HOOK))}",
                            "onFailure": "block",
                        }
                    ],
                }
            ]
        }
    }
)
PREFLIGHT_TIMEOUT = 60
ATTEMPTS = 3
TIMEOUT_SECONDS = 4 * 3600
LARGE_CONTEXT = 200_000
# With every CLAUDE_CODE_* variable but KEPT_CLAUDE_CODE_VARS, what a replay never
# inherits from the session that launched the runner.
SESSION_VARS = {"CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PID"}
# Reason: how the user logs in and which provider serves the model, not markers of
# the launching session; a replay without them would fail or run elsewhere.
KEPT_CLAUDE_CODE_VARS = {
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
}
# Reason: a path is only ever rewritten (or flagged as leaked) when it is NOT
# immediately followed by another path-name character, so a shorter checkout
# path never matches inside a longer, unrelated one (`/x/agent-skills` must
# not touch `/x/agent-skills-other`), while still matching before a quote,
# backtick, parenthesis, colon, semicolon, slash, whitespace or end of string
# (`cd '<top>' && ...`, `` `<top>` ``, `(cd <top>)`, `PYTHONPATH=<top>:x`).
_PATH_BOUNDARY = r"(?![\w.-])"


def _alternation(forms: list[str]) -> str:
    """A non-capturing group of `forms`, so a following boundary lookahead binds
    to every alternative, not just the last one (`|` has the lowest precedence
    of any regex operator)."""
    return "(?:" + "|".join(re.escape(f) for f in forms) + ")"


def project_dir(claude_home: Path, cwd: Path) -> Path:
    """Where Claude Code keeps the sessions of a working directory."""
    return claude_home / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))


def workdir(meta: dict, clone: Path) -> Path:
    """Where the session ran, inside the clone: a job started in a subdirectory
    runs its relative commands from the same place."""
    try:
        return clone / Path(meta["cwd"]).resolve().relative_to(Path(meta["toplevel"]))
    except ValueError:
        return clone


def _worktree_paths(meta: dict) -> list[str]:
    """The checkout's own toplevel, plus (for a linked worktree) the main
    worktree's path: the parent of `git rev-parse --git-common-dir` resolved
    against the toplevel. Toplevel first, since the main path is usually a
    prefix of it and must be replaced second so it cannot swallow a
    toplevel-only occurrence first."""
    top = meta["toplevel"]
    common = run_on_source("git", "-C", top, "rev-parse", "--git-common-dir").strip()
    common_dir = Path(common) if Path(common).is_absolute() else Path(top) / common
    main = str(common_dir.parent.resolve())
    return [top] if main == top else [top, main]


def _path_spellings(path: str) -> list[str]:
    """`path` itself, plus its `~/...` and `$HOME/...` spellings when it sits
    under the home directory."""
    spellings = [path]
    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        rest = path[len(home) :]
        spellings += ["~" + rest, "$HOME" + rest]
    return spellings


def _real_path_forms(tops: list[str]) -> list[str]:
    """The real checkout's own absolute, `~` and `$HOME` spellings (never the
    clone): what a leaked tool-call input would still contain if a rewrite
    were incomplete. A tool call such as `cd $HOME/work/<repo>` uses the
    `$HOME` form just as readily as the other two."""
    return [form for top in tops for form in _path_spellings(top)]


def _rewrite_real_paths(text: str, tops: list[str], clone: Path) -> str:
    """Every spelling of the checkout's own path, and of the main worktree's
    path when this is a linked worktree, rewritten to `clone`, matched only on
    a path boundary. `tops` is computed once by the caller (`_worktree_paths`
    runs `git rev-parse`), never recomputed per transcript line."""
    replacement = str(clone)
    for top in tops:
        pattern = _alternation(_path_spellings(top)) + _PATH_BOUNDARY
        text = re.sub(pattern, lambda _m: replacement, text)
    return text


def _refuse_if_leaked(text: str, leak_forms: list[str]) -> None:
    """Defense in depth: right after every line was rewritten, confirm none of
    the checkout's own spellings survived in the installed copy, before any
    `claude` call spends real time or money on it."""
    if leak_forms and re.search(_alternation(leak_forms) + _PATH_BOUNDARY, text):
        raise Inconclusive("the session copy still names the real repository")


def install_session(snap: Path, clone: Path, claude_home: Path) -> str:
    """Copy the snapshot's conversation in as a new session of the clone.

    Every mention of the original checkout's path (absolute, `~/...`,
    `$HOME/...`, and the main worktree's own path if this is a linked
    worktree) becomes the clone's path, so an agent that reuses one of those
    paths from the conversation edits the clone, never the user's real
    working copy. Raises Inconclusive if a line does not parse as JSON
    (whether it never did, or the rewrite broke it), or if the installed copy
    still names the real repository despite the rewrite."""
    meta = json.loads((snap / "meta.json").read_text())
    tops = _worktree_paths(meta)
    new = str(uuid.uuid4())
    lines, decoded = [], []
    for raw_line in (snap / "transcript.jsonl").read_text().splitlines():
        line = _rewrite_real_paths(raw_line, tops, clone)
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Inconclusive(f"a transcript line did not parse as JSON: {exc}") from exc
        if isinstance(entry, dict) and "sessionId" in entry:
            entry["sessionId"] = new
        # Reason: the leak check reads the ensure_ascii=False text, so a
        # non-ASCII real path is still there to find, not hidden behind a
        # \uXXXX escape. The file gets JSON's default escaping instead: a lone
        # surrogate (Claude Code stores one when it cuts text mid-emoji) has no
        # UTF-8 encoding, so writing it unescaped would raise.
        decoded.append(json.dumps(entry, ensure_ascii=False))
        lines.append(json.dumps(entry))
    _refuse_if_leaked("\n".join(decoded), _real_path_forms(tops))
    target = project_dir(claude_home, workdir(meta, clone))
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{new}.jsonl").write_text("\n".join(lines) + "\n")
    return new


def _turn(entries: list[dict], prompt: str) -> list[dict]:
    """The entries from `prompt`'s own entry to the end: the turn it started.
    A real headless prompt carries no `origin` (that field marks a person
    typing in an interactive session), so the entry is found by its type and
    text alone, not `usage.is_typed`."""
    target = prompt.strip()
    starts = [i for i, e in enumerate(entries) if e.get("type") == "user" and usage.entry_text(e) == target]
    return entries[starts[-1] :] if starts else []


def _session_file_names(pdir: Path) -> set[str]:
    return {p.name for p in pdir.glob("*.jsonl")} if pdir.exists() else set()


def _new_session_files(pdir: Path, before: set[str]) -> list[Path]:
    """The `*.jsonl` files a call created in `pdir`, found by listing the
    directory before and after it, never by trusting its own stdout: a timed-out
    or hard-crashed run never prints its JSON result, but Claude Code writes the
    transcript as it goes, so a killed run can still leave a file to scan."""
    if not pdir.exists():
        return []
    return sorted(p for p in pdir.glob("*.jsonl") if p.name not in before)


def _verify_reported_session(reported: object, new_files: list[Path]) -> None:
    """When the CLI's own JSON does report a session id, it must be one of the
    files the directory listing just found; a mismatch means the two ways of
    identifying the call's session disagree, which is a bug worth raising
    loudly, not a normal replay outcome to route around."""
    if reported and f"{reported}.jsonl" not in {f.name for f in new_files}:
        raise RuntimeError(f"claude reported session {reported}, which is not among the session files it just wrote")


def _subagent_entries(session_file: Path) -> list[dict]:
    """Every entry of the session's subagent transcripts (all new: a subagent
    never inherits the history)."""
    subs_dir = session_file.with_suffix("") / "subagents"
    return [e for sub in sorted(subs_dir.glob("*.jsonl")) for e in usage.read_entries(sub)] if subs_dir.exists() else []


def _new_entries(session_file: Path, prompt: str) -> list[dict]:
    """A call's own entries: its session file's new turn (from `prompt`'s entry
    on, the slice `measure` scores) plus its subagents' transcripts, never the
    inherited history."""
    return _turn(usage.read_entries(session_file), prompt) + _subagent_entries(session_file)


def _hook_rejected(entry: dict, block: dict) -> bool:
    """Whether a tool_result records a call a hook rejected, so its tool never
    ran. Claude Code 2.1.295 records it as an is_error tool_result whose entry
    carries `permissionDecision` {"decision": "reject", "source": "hook"}, the
    same shape whether the hook denied the tool or failed under
    `"onFailure": "block"` (both seen in real transcripts); a tool that ran
    carries {"decision": "accept", ...} instead."""
    decision = entry.get("permissionDecision")
    return (
        block.get("is_error") is True
        and isinstance(decision, dict)
        and decision.get("decision") == "reject"
        and decision.get("source") == "hook"
    )


def _tool_results(entries: list[dict]) -> list[tuple[dict, dict]]:
    """Every tool_result block in `entries`, with the entry that holds it."""
    found = []
    for entry in entries:
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            found += [(entry, b) for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    return found


def _ran_a_tool(new_files: list[Path], prompt: str) -> bool:
    """Whether a tool actually ran in any of `new_files`' own entries
    (`_new_entries`): any tool_result that is not a hook's rejection. A result
    that does not look exactly like one counts as a tool that ran, and so does
    a non-empty file with no entry carrying `prompt` (Ruling R4: its new turn
    cannot be told from the history, so nothing shows that no tool ran), so a
    transcript this does not understand fails closed."""
    for f in new_files:
        entries = usage.read_entries(f)
        turn = _turn(entries, prompt)
        if f.stat().st_size and not turn:
            return True
        if any(not _hook_rejected(entry, block) for entry, block in _tool_results(turn + _subagent_entries(f))):
            return True
    return False


def _hook_commands() -> list[str]:
    """Every hook command SETTINGS gives Claude Code, read back from the very
    string every call passes."""
    groups = json.loads(SETTINGS)["hooks"]["PreToolUse"]
    return [hook["command"] for group in groups for hook in group["hooks"]]


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return str(content or "")


def _guard_blocked_a_tool(new_files: list[Path], prompt: str) -> bool:
    """Whether the warm-up guard blocked a tool in a job (Ruling R5). The guard
    prints nothing in a job, so a hook rejection there that names one of its
    commands (Claude Code words a hook failure under `"onFailure": "block"` as
    `[<command>]: failed; blocking because onFailure is "block"`) can only be the
    guard failing. Rejections by any other hook (the user's own may deny tools
    in a real job) are left alone."""
    names = [f"[{command}]" for command in _hook_commands()]
    return any(
        _hook_rejected(entry, block) and any(name in _result_text(block) for name in names)
        for f in new_files
        for entry, block in _tool_results(_new_entries(f, prompt))
    )


def _leaked_real_path(new_files: list[Path], prompt: str, leak_forms: list[str]) -> bool:
    """Whether a tool call in any of `new_files`' own new turn, or in their
    subagent transcripts, still names the real checkout. `new_files` (Open 1b)
    are the session files one call just created, found by listing the project
    directory rather than trusting stdout, so a timed-out or hard-crashed call
    is scanned exactly like a clean one. Scoped to each file's own new turn
    (the same slice `measure` scores), never its whole inherited history: a
    history that merely mentions an unrelated sibling checkout
    (`<repo>-flowchart` next to `<repo>`) must never be mistaken for a leak."""
    if not leak_forms or not new_files:
        return False
    pattern = re.compile(_alternation(leak_forms) + _PATH_BOUNDARY)
    for f in new_files:
        for entry in _new_entries(f, prompt):
            if entry.get("type") != "assistant":
                continue
            for block in (entry.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # Reason: ensure_ascii=False, so a non-ASCII path still matches
                    # instead of surviving only as an escaped \uXXXX sequence.
                    text = json.dumps(block.get("input") or {}, ensure_ascii=False)
                    if pattern.search(text):
                        return True
    return False


class Terminated(BaseException):
    """SIGTERM or SIGHUP during a `claude` call (closing the terminal is the
    common way a long replay dies), raised so the call's process group is
    stopped on the way out, as for Ctrl-C."""


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _raise_terminated(signum: int, frame: object) -> None:
    raise Terminated(f"signal {signum}")


def _stop_on_signals() -> dict:
    """Turn SIGTERM and SIGHUP into Terminated for the length of a call, and
    return the handlers to put back (none off the main thread, where Python
    cannot set one). A signal the runner ignores stays ignored: under `nohup`,
    SIGHUP is SIG_IGN, so closing the terminal must not stop the replay."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {
        sig: signal.signal(sig, _raise_terminated) for sig in STOP_SIGNALS if signal.getsignal(sig) != signal.SIG_IGN
    }


def _put_back(previous: dict) -> None:
    for sig, handler in previous.items():
        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


class IdentityUnavailable(RuntimeError):
    """`ps` could not run, so a recorded process group cannot be checked."""


def _identity(pid: int) -> tuple[str, str] | None:
    """(start time, command line) of `pid`, as `ps` shows them, or None when no
    such process runs. Raises IdentityUnavailable when `ps` itself cannot run
    or answers in a shape this cannot read."""
    try:
        shown = subprocess.run(
            ["ps", "-ww", "-o", "lstart=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityUnavailable(f"ps could not run: {exc}") from exc
    line = shown.stdout.strip()
    if not line:
        # Reason: for a pid that does not exist, ps prints nothing, not even an
        # error; anything on stderr means ps itself failed.
        if shown.stderr.strip():
            raise IdentityUnavailable(f"ps failed: {shown.stderr.strip()[:200]}")
        return None
    parts = line.split(None, 5)
    if len(parts) < 6:
        raise IdentityUnavailable(f"ps answered {line!r}")
    return " ".join(parts[:5]), parts[5]


class UnverifiableGroup(RuntimeError):
    """A group record that cannot be settled either way: unreadable, or naming a
    group that still runs without its leader. A person has to look."""


def _record_group(record: Path, pgid: int) -> None:
    """Next to the group id, its leader's start time and command line, so a later
    run kills the group only if it is still this very `claude` (Ruling F23d):
    after a reboot, or on a machine whose pid space wrapped, the id can belong
    to something else entirely. Written to a temp file in the same folder and
    then renamed (Ruling F30), so a hard kill never leaves half a record."""
    identity = _identity(pgid)
    if identity is not None:
        temp = record.with_name(f"{record.name}.tmp")
        temp.write_text(json.dumps({"pgid": pgid, "lstart": identity[0], "command": identity[1]}) + "\n")
        os.replace(temp, record)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_leftover_groups(results: Path) -> list[int]:
    """Kill the `claude` process groups an earlier run of this job recorded under
    `results` and never stopped (it was killed outright), but only a group whose
    leader `ps` still shows with the recorded start time and command line, a
    `claude --fork-session` call. Returns the groups killed.

    A record whose leader is gone, or shows as another process, is dropped
    without a kill (with an old bare-number record, Ruling F25), unless the
    group still runs without its leader, such as a dev server the `claude`
    started (Ruling F30): that group cannot be checked, so this raises
    UnverifiableGroup, as it does for an unreadable record. It raises
    IdentityUnavailable when `ps` cannot run. A resume must not go on without
    knowing."""
    stopped = []
    for record in sorted(results.glob("side-*/attempt-*/claude.pgid")):
        unreadable = UnverifiableGroup(
            f"the group record {record} cannot be read. Stop any claude an earlier run left yourself, delete the "
            f"record, and run replay again. Inspect the record with: cat {shlex.quote(str(record))}"
        )
        try:
            recorded = json.loads(record.read_text())
        except (OSError, ValueError) as exc:
            raise unreadable from exc
        if type(recorded) is int:
            record.unlink()
            continue
        try:
            pgid, lstart, command = int(recorded["pgid"]), str(recorded["lstart"]), str(recorded["command"])
        except (KeyError, TypeError, ValueError) as exc:
            raise unreadable from exc
        if pgid <= 1:
            raise unreadable
        # Reason: no other group can hold the id of the runner's own group, so a
        # record naming it is stale.
        if pgid != os.getpgrp():
            current = _identity(pgid)
            if current == (lstart, command) and "--fork-session" in command:
                _kill_group(pgid)
                stopped.append(pgid)
            elif current is None and _group_exists(pgid):
                raise UnverifiableGroup(
                    f"process group {pgid}, recorded in {record}, still runs without its leader, so it cannot be "
                    "checked. Stop it yourself if an earlier run left it, delete the record, and run replay again. "
                    f"Inspect the group with: ps -o pid,pgid,lstart,command -g {pgid}"
                )
        record.unlink()
    return stopped


def run_claude(
    clone: Path,
    session_id: str,
    prompt: str,
    env_extra: dict,
    claude_home: Path,
    model: str,
    pgid_file: Path | None = None,
) -> tuple[dict, float, int, bool]:
    """One headless fork of the session, with the same access as the user's own
    session. The prompt is the last argument, after `--`, so a message that
    starts with `-` is never parsed as an option. Every call gets the same
    `--settings` (SETTINGS), so a warm-up and its job differ only in their
    environment, never in their argv. A key in `env_extra` mapped
    to None is removed from the environment instead of set, so a call can
    strip a variable it must never inherit. The fourth return value is True
    only for a call this killed after TIMEOUT_SECONDS; the exit code next to it
    is whatever the killed process actually reported (often a negative signal
    number), never a stand-in like -1, which is also SIGHUP's own code. On
    Ctrl-C, SIGTERM, SIGHUP or any other exception during the wait, the call's
    whole process group is killed before the exception goes on. While the call
    runs, `pgid_file` (if given) records its process group and the leader's
    identity."""
    cmd = [
        os.environ.get("JEV_FORK_CHECK_CLAUDE", "claude"),
        "--resume",
        session_id,
        "--fork-session",
        "-p",
        "--output-format",
        "json",
        "--settings",
        SETTINGS,
        "--permission-mode",
        "bypassPermissions",
        "--model",
        model,
        "--",
        prompt,
    ]
    # Reason: the runner itself usually runs inside a Claude Code session, whose
    # own markers (session id, messaging socket and token, child-session and
    # bridge ids, effort, pid) would tie the replay to that session. Everything
    # else, CLAUDE_CONFIG_DIR, PATH, HOME and the auth and provider variables in
    # KEPT_CLAUDE_CODE_VARS included, is kept.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in SESSION_VARS and (not k.startswith("CLAUDE_CODE_") or k in KEPT_CLAUDE_CODE_VARS)
    }
    env["JEV_ROUTER"] = "off"
    for key, value in env_extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    # Reason: setting CLAUDE_CONFIG_DIR, even to the default, moves where Claude Code
    # reads its settings and MCP servers, so it is set only for a test home.
    if claude_home != DEFAULT_CLAUDE_HOME:
        env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    started = time.monotonic()
    timed_out = False
    previous = _stop_on_signals()
    try:
        # Reason: a new session (not just a new process group) is what lets a
        # timed-out run be killed as a whole, so an MCP or dev server the agent
        # started as a child of it does not keep running after `claude` itself is
        # gone. It also keeps the terminal's Ctrl-C from reaching the child: the
        # runner stops the group itself.
        proc = subprocess.Popen(
            cmd, cwd=clone, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
        )
        try:
            # Reason: a runner killed outright (SIGKILL, a crash) cannot stop the
            # group; the next run of the job finds it here (`stop_leftover_groups`).
            # Written inside this guard, so a failed write still kills the group.
            if pgid_file is not None:
                _record_group(pgid_file, proc.pid)
            stdout, _ = proc.communicate(timeout=TIMEOUT_SECONDS)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid)
            proc.wait()
            stdout, code, timed_out = "", proc.returncode, True
        except BaseException:  # Ctrl-C, SIGTERM or SIGHUP (as Terminated), a failed write, anything
            _kill_group(proc.pid)
            proc.wait()
            raise
    finally:
        _put_back(previous)
        if pgid_file is not None:
            pgid_file.unlink(missing_ok=True)
    wall = time.monotonic() - started
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = {}
    return (data if isinstance(data, dict) else {}), wall, code, timed_out


def measure(claude_home: Path, clone: Path, session_id: str, prompt: str, helper: str) -> dict:
    pdir = project_dir(claude_home, clone)
    path = pdir / f"{session_id}.jsonl"
    entries = usage.read_entries(path) if session_id and path.exists() else []
    turn = _turn(entries, prompt)
    main = usage.calls(turn)
    subs = (
        [
            c
            for f in sorted((pdir / session_id / "subagents").glob("*.jsonl"))
            for c in usage.calls(usage.read_entries(f), sidechain=True)
        ]
        if session_id
        else []
    )
    prices = usage.load_prices()
    costs = [c.cost(prices) for c in main + subs]
    handed = any(
        b.get("type") == "tool_use"
        and b.get("name") in ("Agent", "Task")
        and (b.get("input") or {}).get("subagent_type") == helper
        for e in turn
        if e.get("type") == "assistant"
        for b in (e.get("message") or {}).get("content") or []
        if isinstance(b, dict)
    )
    return {
        "calls": len(main) + len(subs),
        "cost": sum(c for c in costs if c is not None),
        "unpriced_calls": sum(c is None for c in costs),
        "first_cache_read": main[0].read if main else 0,
        "first_context": main[0].context if main else 0,
        "delegated": handed,
    }


def _run_hook(command: str, env: dict) -> subprocess.CompletedProcess[str] | None:
    """One run of a hook command through the shell, as a PreToolUse hook gets it;
    None if it could not run or did not finish in time."""
    hook_input = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}})
    try:
        return subprocess.run(
            command, shell=True, env=env, input=hook_input, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def warmup_guard_passes_preflight() -> bool:
    """Whether the warm-up guard works, checked before any `claude` runs by
    running every hook command SETTINGS holds, read back from that very string so
    it is exactly what the calls will run. With WARMUP_VAR set to "1" each must
    exit 0 and print the stop-and-deny decision (`warmup_hook.STOP`); without
    it, each must exit 0 and print nothing. So the guard can neither let a
    warm-up's tool run nor stop a job."""
    commands = _hook_commands()
    job_env = {k: v for k, v in os.environ.items() if k != WARMUP_VAR}
    warmup_env = {**job_env, WARMUP_VAR: "1"}
    for command in commands:
        warm, job = _run_hook(command, warmup_env), _run_hook(command, job_env)
        if warm is None or job is None or warm.returncode != 0 or job.returncode != 0 or job.stdout:
            return False
        try:
            if json.loads(warm.stdout) != warmup_hook.STOP:
                return False
        except json.JSONDecodeError:
            return False
    return bool(commands)


def _replay_side(
    snap: Path, work: Path, claude_home: Path, meta: dict, prompt: str, model: str, name: str, position: int
) -> tuple[dict, str]:
    """Run one side (keep or delegate) up to ATTEMPTS times, each in a fresh
    clone, until the warm-up and the job both prove the cache was hot and
    neither crashes, times out, nor touches the real checkout. Returns the
    last attempt's result and, if the side never became scoreable, why. A
    leak, a warm-up that ran a tool, a timeout or an unpriced call ends the
    side (and, via the caller, the whole pair) at once, without retrying: none
    of them would be fixed by a fresh clone."""
    note = {"JEV_ROUTER_NOTE_FILE": str(snap / "note.txt") if name == "delegate" else None}
    # Reason: so that the warm-up's first request can match the job's (a cache
    # entry ends with the request that wrote it), the warm-up gets exactly the
    # job's environment, the note included, and differs only by WARMUP_VAR,
    # which makes warmup_hook.py stop it before any tool runs. Whether the job
    # then really read the cache is checked on every attempt below. The job
    # removes the variable, so a value inherited from the runner never turns it
    # into a warm-up.
    job_env = {**note, WARMUP_VAR: None}
    warmup_env = {**note, WARMUP_VAR: "1"}
    side: dict = {}
    reason = ""
    for attempt in range(1, ATTEMPTS + 1):
        # Reason: folders are named by run order, which is random, so a path
        # never tells the judge which side made a result.
        dest = work / f"side-{position}" / f"attempt-{attempt}"
        clone = restore(snap, dest)
        tops = _worktree_paths(meta)
        leak_forms = _real_path_forms(tops)
        sid = install_session(snap, clone, claude_home)
        cwd = workdir(meta, clone)
        pdir = project_dir(claude_home, cwd)
        # Reason: a message naming the real checkout (e.g. "fix ~/work/x/y.py")
        # must never reach `claude` verbatim: under bypass permissions it would
        # send the job straight into the real repository.
        job_prompt = _rewrite_real_paths(prompt, tops, clone)
        restored = dest / "restore.json"
        skipped = json.loads(restored.read_text())["skipped"] if restored.exists() else []
        side = {"attempt": attempt, "clone": str(clone), "warm": False, "skipped": skipped}
        before_status = run("git", "-C", str(clone), "status", "--porcelain", "-uall")
        before_head = run("git", "-C", str(clone), "rev-parse", "HEAD").strip()
        # Reason: the warm-up is launched exactly like the job (same session,
        # message, settings and model), so the job's first call can read its
        # prefix from cache; the hook stops the warm-up before its first tool.
        before_files = _session_file_names(pdir)
        warm_data, _, warm_code, warm_timed_out = run_claude(
            cwd, sid, job_prompt, warmup_env, claude_home, model, pgid_file=dest / "claude.pgid"
        )
        warm_new_files = _new_session_files(pdir, before_files)
        # Reason: every attempt is scanned, whether it succeeded, crashed or
        # timed out, since a leak can happen before a call ever fails. The scan
        # comes before the session-id check, so a call whose reported id does
        # not match is still scanned rather than lost to that error.
        if _leaked_real_path(warm_new_files, job_prompt, leak_forms):
            return side, "a replay used a path into the real repository"
        # Reason (Ruling R1): a guard that failed or was skipped lets the warm-up
        # run the job's own tools under bypass permissions, and the clone check
        # below cannot see a network call, an ignored file or a write to a file
        # that was already dirty. Like a leak, it ends the pair at once: an
        # effect may already have happened, and a fresh clone does not mend the
        # guard. Scanned like the leak, before the session-id check.
        if _ran_a_tool(warm_new_files, job_prompt):
            return side, "warm-up ran a tool"
        _verify_reported_session(warm_data.get("session_id"), warm_new_files)
        if warm_timed_out:
            return side, "timed out"
        if warm_code != 0 or warm_data.get("is_error"):
            reason = "crashed"
            continue
        after_status = run("git", "-C", str(clone), "status", "--porcelain", "-uall")
        after_head = run("git", "-C", str(clone), "rev-parse", "HEAD").strip()
        if after_status != before_status or after_head != before_head:
            reason = "warm-up changed the clone"
            continue
        warm_sid = str(warm_data.get("session_id") or "")
        warm = measure(claude_home, cwd, warm_sid, job_prompt, "")
        if warm["calls"] == 0:
            reason = "never warm"
            continue
        # Reason: an unpriced call is never retried (a fresh clone would price
        # it the same way), and it must stop the other side too.
        if warm["unpriced_calls"]:
            return side, "unpriced"
        before_files = _session_file_names(pdir)
        data, wall, code, job_timed_out = run_claude(
            cwd, sid, job_prompt, job_env, claude_home, model, pgid_file=dest / "claude.pgid"
        )
        job_new_files = _new_session_files(pdir, before_files)
        if _leaked_real_path(job_new_files, job_prompt, leak_forms):
            return side, "a replay used a path into the real repository"
        _verify_reported_session(data.get("session_id"), job_new_files)
        if job_timed_out:
            return side, "timed out"
        # Reason (Ruling R5): if the guard failed during the job, its
        # `"onFailure": "block"` rejected the job's own tools, so the job did
        # not run as it really would; retried like a cold attempt.
        if _guard_blocked_a_tool(job_new_files, job_prompt):
            reason = "warm-up guard blocked a job tool"
            continue
        if code != 0 or data.get("is_error"):
            reason = "crashed"
            continue
        job_sid = str(data.get("session_id") or "")
        m = measure(claude_home, cwd, job_sid, job_prompt, meta["helper"])
        # Reason: the warm-up's own calls being priced does not prove the JOB's
        # first call actually read the shared prefix from cache; both are checked.
        if m["first_cache_read"] < 0.99 * warm["first_context"] - 2_000:
            reason = "never warm"
            continue
        side = {
            "attempt": attempt,
            "clone": str(clone),
            "session_id": data.get("session_id"),
            "wall_seconds": round(wall, 1),
            "exit_code": code,
            "warm": True,
            "skipped": skipped,
            **m,
        }
        # Reason: a call with no price would count as free and flatter one side.
        if m["unpriced_calls"]:
            return side, "unpriced"
        return side, ""
    return side, reason


def replay_pair(snap: Path, work: Path, claude_home: Path, rng: random.Random) -> dict:
    """Keep and delegate, one after the other in random order, each in a fresh clone
    with a verified warm cache. Once one side is inconclusive, the other never runs:
    it cannot change the answer, and every attempt costs real time and money."""
    meta = json.loads((snap / "meta.json").read_text())
    order = ["keep", "delegate"]
    rng.shuffle(order)
    if not meta.get("model"):
        return {"id": meta["id"], "order": order, "sides": {}, "inconclusive": True, "reason": "no model recorded"}
    # Reason (Ruling R1): the guard is all that keeps a warm-up from running the
    # job's tools under bypass permissions, so it is proven before anything runs.
    if not warmup_guard_passes_preflight():
        return {
            "id": meta["id"],
            "order": order,
            "sides": {},
            "inconclusive": True,
            "reason": "warm-up guard failed its preflight",
        }
    model = meta["model"] + ("[1m]" if (meta.get("context") or 0) > LARGE_CONTEXT else "")
    prompt = (snap / "message.txt").read_text()
    sides: dict[str, dict] = {}
    reason = ""
    for position, name in enumerate(order, 1):
        side, side_reason = _replay_side(snap, work, claude_home, meta, prompt, model, name, position)
        sides[name] = side
        if side_reason:
            reason = side_reason
            break
    return {"id": meta["id"], "order": order, "sides": sides, "inconclusive": bool(reason), "reason": reason}
