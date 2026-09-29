"""Replay a snapshot both ways: restore it into clones, run keep and delegate, measure."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import snapshot
import usage

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
    if any(path.startswith(prefix) for path in excluded):
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
    gone since) and `not_captured` (ignored now but new since the capture, never
    copied: a secret added to info/exclude, a cache, the real turn's output)."""
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
    run_on_source("git", "clone", "-q", "--bare", str(top), str(bare))
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
        for rel in captured:
            if not os.path.lexists(top / rel):
                missing.append(rel)
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
        # the content check still knows a `.env` the replay moved away (Ruling F10).
        contents = _restored_files(clone, ignored)
        record = {
            "skipped": sorted(skipped),
            "ignored": sorted(ignored),
            "missing": missing,
            "not_captured": not_captured,
            "ignored_blobs": dict(zip(_blob_ids(clone, contents), contents, strict=True)),
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


# The content check covers restored ignored files of this size: under 8 bytes is
# too little to be a secret worth refusing a job for, and over 1 MB is a build
# output or a cache, not a credential.
CONTENT_MIN_BYTES = 8
CONTENT_MAX_BYTES = 1 << 20


def _restored_files(clone: Path, ignored: list[str]) -> list[str]:
    """The regular files now under the restored ignored entries, relative to the
    clone, outside dependency folders and between the two content-check sizes.
    Nothing here follows a symlink."""
    found: list[str] = []

    def consider(rel: str) -> None:
        st = os.lstat(clone / rel)
        if stat.S_ISREG(st.st_mode) and CONTENT_MIN_BYTES <= st.st_size <= CONTENT_MAX_BYTES and "\n" not in rel:
            found.append(rel)

    for rel in ignored:
        path = clone / rel
        if not os.path.lexists(path) or any(part in snapshot.DEPENDENCY_DIRS for part in Path(rel).parts):
            continue
        if path.is_symlink() or not path.is_dir():
            consider(rel)
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [name for name in dirnames if name not in snapshot.DEPENDENCY_DIRS]
            base = os.path.relpath(dirpath, clone)
            for name in filenames:
                consider(f"{base}/{name}")
    return found


def _blob_ids(clone: Path, paths: list[str]) -> list[str]:
    """The git blob id of each of `paths` (relative to the clone), in one call."""
    if not paths:
        return []
    result = subprocess.run(
        ["git", "-C", str(clone), "hash-object", "--stdin-paths"],
        input="".join(f"{p}\n" for p in paths),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git hash-object failed: {result.stderr.strip()[:300]}")
    return result.stdout.split()


def _refuse_if_content_leaked(clone: Path, tree: list[tuple[str, str, str]], status: list[str], label: str) -> None:
    """Refuses if a restored ignored file's exact content shows up under another
    path: in HEAD's tree, in any object the replay's commits introduced, or in a
    working-tree file `git status` lists. A replay that copies or moves `.env` to
    `config.txt` would otherwise publish the secret, or show it to the judge,
    under a name no path check knows. The restored files' ids come from
    `restore.json`, hashed before the replay ran, so a moved file still counts;
    only the working-tree files are hashed here."""
    secrets = restored_blobs(clone)
    # Reason (accepted in Ruling F11): content already in the start state is
    # published anyway (it sits in the base history or the start tree), so
    # matching it leaks nothing new; a `.env` copied from a tracked
    # `.env.example` must not refuse every job.
    for kind, oid, _ in _tree(clone, "refs/jev/start"):
        if kind == "blob":
            secrets.pop(oid, None)
    if not secrets:
        return
    worktree = [p for p in status if (clone / p).is_file() and not (clone / p).is_symlink() and "\n" not in p]
    introduced = run("git", "-C", str(clone), "rev-list", "--objects", "refs/jev/start..HEAD")
    candidates = [(oid, path) for kind, oid, path in tree if kind == "blob"]
    for line in introduced.splitlines():
        oid, _, path = line.partition(" ")
        candidates.append((oid, path or "(no path)"))
    candidates += list(zip(_blob_ids(clone, worktree), worktree, strict=True))
    for oid, path in candidates:
        if oid in secrets:
            raise RuntimeError(
                f"{path} in the {label} clone holds the content of the restored ignored file {secrets[oid]}; refusing"
            )


def refuse_if_ignored_leaked(clone: Path, ignored: list[str], label: str) -> None:
    """Refuses if any of `ignored` (or anything under it) is committed at HEAD, was
    ever touched between `refs/jev/start` (where the replay began) and HEAD, or
    merely sits in the working tree right now: a session with bypass permissions
    can drop a `.gitignore` line or force-add a path, so the clone's own current
    ignore rules cannot be trusted; only what `restore` actually copied in can.
    The working-tree check is what catches this before anything is ever
    committed, which matters to the judge: it never commits, so an un-ignored
    `.env` would otherwise show nowhere else. Then the same places are checked
    for a restored ignored file's content under another name
    (`_refuse_if_content_leaked`).

    All three listings are read with `-z`: without it, git quotes a path that
    holds a non-ASCII byte, a `"`, a backslash or a control character, so the
    quoted form would never equal the plain name recorded in `restore.json`
    and the check would silently miss it."""
    if not ignored:
        return
    tree = _tree(clone, "HEAD")
    # --diff-merges=m: a path introduced only by how a merge resolved a
    # conflict is still listed, not skipped as merges normally are.
    history_out = run(
        "git", "-C", str(clone), "log", "--name-only", "-z", "--format=", "--diff-merges=m", "refs/jev/start..HEAD"
    )
    history = [p for p in history_out.split("\0") if p]
    status_out = run("git", "-C", str(clone), "status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    status = _status_paths(status_out)
    for path in [p for _, _, p in tree] + history + status:
        hit = _matches_ignored(path, ignored)
        if hit:
            raise RuntimeError(f"{hit} would be published or shown from the {label} clone (found as {path}); refusing")
    _refuse_if_content_leaked(clone, tree, status, label)


DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
WARMUP = "Reply with the single word ok and do nothing else."
ATTEMPTS = 3
TIMEOUT_SECONDS = 4 * 3600
LARGE_CONTEXT = 200_000
# With every CLAUDE_CODE_* variable, what a replay never inherits from the session
# that launched the runner.
SESSION_VARS = {"CLAUDECODE", "CLAUDE_EFFORT", "CLAUDE_PID"}
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
        turn = _turn(usage.read_entries(f), prompt)
        subs_dir = f.with_suffix("") / "subagents"
        subs = (
            [e for sub in sorted(subs_dir.glob("*.jsonl")) for e in usage.read_entries(sub)]
            if subs_dir.exists()
            else []
        )
        for entry in turn + subs:
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


def run_claude(
    clone: Path, session_id: str, prompt: str, env_extra: dict, claude_home: Path, model: str
) -> tuple[dict, float, int, bool]:
    """One headless fork of the session, with the same access as the user's own
    session. The prompt is the last argument, after `--`, so a message that
    starts with `-` is never parsed as an option. A key in `env_extra` mapped
    to None is removed from the environment instead of set, so a call can
    strip a variable it must never inherit. The fourth return value is True
    only for a call this killed after TIMEOUT_SECONDS; the exit code next to it
    is whatever the killed process actually reported (often a negative signal
    number), never a stand-in like -1, which is also SIGHUP's own code."""
    cmd = [
        os.environ.get("JEV_FORK_CHECK_CLAUDE", "claude"),
        "--resume",
        session_id,
        "--fork-session",
        "-p",
        "--output-format",
        "json",
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
    # else, CLAUDE_CONFIG_DIR, PATH and HOME included, is kept.
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS and not k.startswith("CLAUDE_CODE_")}
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
    # Reason: a new session (not just a new process group) is what lets a timed-out
    # run be killed as a whole, so an MCP or dev server the agent started as a child
    # of it does not keep running after `claude` itself is gone.
    proc = subprocess.Popen(
        cmd, cwd=clone, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    timed_out = False
    try:
        stdout, _ = proc.communicate(timeout=TIMEOUT_SECONDS)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        stdout, code, timed_out = "", proc.returncode, True
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


def _replay_side(
    snap: Path, work: Path, claude_home: Path, meta: dict, prompt: str, model: str, name: str, position: int
) -> tuple[dict, str]:
    """Run one side (keep or delegate) up to ATTEMPTS times, each in a fresh
    clone, until the warm-up and the job both prove the cache was hot and
    neither crashes, times out, nor touches the real checkout. Returns the
    last attempt's result and, if the side never became scoreable, why. A
    leak, a timeout or an unpriced call ends the side (and, via the caller,
    the whole pair) at once, without retrying: none of the three would be
    fixed by a fresh clone."""
    job_env = {"JEV_ROUTER_NOTE_FILE": str(snap / "note.txt")} if name == "delegate" else {"JEV_ROUTER_NOTE_FILE": None}
    # Reason: the warm-up is never the job; it must never be handed the note,
    # on either side, or a hook that returns the note for any prompt would
    # tell the "reply ok" warm-up itself to delegate.
    warmup_env = {"JEV_ROUTER_NOTE_FILE": None}
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
        # Reason: the warm-up forks the same conversation with the same tools
        # and system prompt, so the side's first call can read it from cache.
        before_files = _session_file_names(pdir)
        warm_data, _, warm_code, warm_timed_out = run_claude(cwd, sid, WARMUP, warmup_env, claude_home, model)
        warm_new_files = _new_session_files(pdir, before_files)
        # Reason: every attempt is scanned, whether it succeeded, crashed or
        # timed out, since a leak can happen before a call ever fails. The scan
        # comes before the session-id check, so a call whose reported id does
        # not match is still scanned rather than lost to that error.
        if _leaked_real_path(warm_new_files, WARMUP, leak_forms):
            return side, "a replay used a path into the real repository"
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
        warm = measure(claude_home, cwd, warm_sid, WARMUP, "")
        if warm["calls"] == 0:
            reason = "never warm"
            continue
        # Reason: an unpriced call is never retried (a fresh clone would price
        # it the same way), and it must stop the other side too.
        if warm["unpriced_calls"]:
            return side, "unpriced"
        before_files = _session_file_names(pdir)
        data, wall, code, job_timed_out = run_claude(cwd, sid, job_prompt, job_env, claude_home, model)
        job_new_files = _new_session_files(pdir, before_files)
        if _leaked_real_path(job_new_files, job_prompt, leak_forms):
            return side, "a replay used a path into the real repository"
        _verify_reported_session(data.get("session_id"), job_new_files)
        if job_timed_out:
            return side, "timed out"
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
