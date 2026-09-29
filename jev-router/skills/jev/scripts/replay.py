"""Replay a snapshot both ways: restore it into clones, run keep and delegate, measure."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

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


DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
WARMUP = "Reply with the single word ok and do nothing else."
ATTEMPTS = 3
TIMEOUT_SECONDS = 4 * 3600
LARGE_CONTEXT = 200_000
# Reason: a path is only ever rewritten (or flagged as leaked) up to this
# boundary, so a shorter checkout path never matches inside a longer,
# unrelated one (`/x/agent-skills` must not touch `/x/agent-skills-other`).
_PATH_BOUNDARY = r'(?=[/"\s\\]|$)'


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
    common = run("git", "-C", top, "rev-parse", "--git-common-dir").strip()
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


def _real_path_forms(meta: dict) -> list[str]:
    """The real checkout's own absolute and `~` spellings (never the `$HOME`
    form, and never the clone): what a leaked tool-call input would still
    contain if a rewrite were incomplete."""
    return [form for top in _worktree_paths(meta) for form in _path_spellings(top)[:2]]


def _rewrite_real_paths(text: str, meta: dict, clone: Path) -> str:
    """Every spelling of the checkout's own path, and of the main worktree's
    path when this is a linked worktree, rewritten to `clone`, matched only on
    a path boundary."""
    replacement = str(clone)
    for top in _worktree_paths(meta):
        for form in _path_spellings(top):
            text = re.sub(re.escape(form) + _PATH_BOUNDARY, lambda _m: replacement, text)
    return text


def install_session(snap: Path, clone: Path, claude_home: Path) -> str:
    """Copy the snapshot's conversation in as a new session of the clone.

    Every mention of the original checkout's path (absolute, `~/...`,
    `$HOME/...`, and the main worktree's own path if this is a linked
    worktree) becomes the clone's path, so an agent that reuses one of those
    paths from the conversation edits the clone, never the user's real
    working copy."""
    meta = json.loads((snap / "meta.json").read_text())
    new = str(uuid.uuid4())
    lines = []
    for raw_line in (snap / "transcript.jsonl").read_text().splitlines():
        line = _rewrite_real_paths(raw_line, meta, clone)
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Inconclusive(f"a transcript line no longer parsed after rewriting its paths: {exc}") from exc
        if isinstance(entry, dict) and "sessionId" in entry:
            entry["sessionId"] = new
        lines.append(json.dumps(entry))
    target = project_dir(claude_home, workdir(meta, clone))
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{new}.jsonl").write_text("\n".join(lines) + "\n")
    return new


def _leaked_real_path(claude_home: Path, cwd: Path, session_id: str, forms: list[str]) -> bool:
    """Whether any tool call in `session_id`'s main or subagent transcripts
    still names the real checkout, by any of `forms`. A hit means the rewrite
    in `install_session` was incomplete and, since replays run with bypass
    permissions, the job may have edited the user's real working copy."""
    if not session_id or not forms:
        return False
    pdir = project_dir(claude_home, cwd)
    paths = [pdir / f"{session_id}.jsonl", *sorted((pdir / session_id / "subagents").glob("*.jsonl"))]
    for path in paths:
        if not path.exists():
            continue
        for entry in usage.read_entries(path):
            if entry.get("type") != "assistant":
                continue
            for block in (entry.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    text = json.dumps(block.get("input") or {})
                    if any(form in text for form in forms):
                        return True
    return False


def run_claude(
    clone: Path, session_id: str, prompt: str, env_extra: dict, claude_home: Path, model: str
) -> tuple[dict, float, int]:
    """One headless fork of the session, with the same access as the user's own
    session. The prompt is the last argument, after `--`, so a message that
    starts with `-` is never parsed as an option. A key in `env_extra` mapped
    to None is removed from the environment instead of set, so a side can
    strip a variable it must never inherit."""
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
    env = dict(os.environ)
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
    try:
        stdout, _ = proc.communicate(timeout=TIMEOUT_SECONDS)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        stdout, code = "", -1
    wall = time.monotonic() - started
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = {}
    return (data if isinstance(data, dict) else {}), wall, code


def measure(claude_home: Path, clone: Path, session_id: str, prompt: str, helper: str) -> dict:
    pdir = project_dir(claude_home, clone)
    path = pdir / f"{session_id}.jsonl"
    entries = usage.read_entries(path) if session_id and path.exists() else []
    target = prompt.strip()
    # Reason: a real headless prompt carries no `origin` (that field marks a
    # person typing in an interactive session), so the prompt turn is found by
    # its type and text alone, not `usage.is_typed`.
    starts = [i for i, e in enumerate(entries) if e.get("type") == "user" and usage.entry_text(e) == target]
    turn = entries[starts[-1] :] if starts else []
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
    clone, until the warm-up proves the cache is hot and the job neither
    crashes nor touches the real checkout. Returns the last attempt's result
    and, if the side never became scoreable, why."""
    note_env = (
        {"JEV_ROUTER_NOTE_FILE": str(snap / "note.txt")} if name == "delegate" else {"JEV_ROUTER_NOTE_FILE": None}
    )
    leak_forms = _real_path_forms(meta)
    side: dict = {}
    reason = ""
    for attempt in range(1, ATTEMPTS + 1):
        # Reason: folders are named by run order, which is random, so a path
        # never tells the judge which side made a result.
        dest = work / f"side-{position}" / f"attempt-{attempt}"
        clone = restore(snap, dest)
        sid = install_session(snap, clone, claude_home)
        cwd = workdir(meta, clone)
        restored = dest / "restore.json"
        skipped = json.loads(restored.read_text())["skipped"] if restored.exists() else []
        side = {"attempt": attempt, "clone": str(clone), "warm": False, "skipped": skipped}
        before_status = run("git", "-C", str(clone), "status", "--porcelain", "-uall")
        before_head = run("git", "-C", str(clone), "rev-parse", "HEAD").strip()
        # Reason: the warm-up forks the same conversation with the same tools
        # and system prompt, so the side's first call can read it from cache.
        warm_data, _, warm_code = run_claude(cwd, sid, WARMUP, note_env, claude_home, model)
        if warm_code != 0 or warm_data.get("is_error"):
            reason = "crashed"
            continue
        after_status = run("git", "-C", str(clone), "status", "--porcelain", "-uall")
        after_head = run("git", "-C", str(clone), "rev-parse", "HEAD").strip()
        if after_status != before_status or after_head != before_head:
            reason = "warm-up changed the clone"
            continue
        warm = measure(claude_home, cwd, str(warm_data.get("session_id") or ""), WARMUP, "")
        if warm["calls"] == 0 or warm["unpriced_calls"]:
            reason = "never warm"
            continue
        data, wall, code = run_claude(cwd, sid, prompt, note_env, claude_home, model)
        if code != 0 or data.get("is_error"):
            reason = "crashed"
            continue
        session_id = str(data.get("session_id") or "")
        if _leaked_real_path(claude_home, cwd, session_id, leak_forms):
            return side, "a replay used a path into the real repository"
        m = measure(claude_home, cwd, session_id, prompt, meta["helper"])
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
    # Reason: a call with no price would count as free and flatter one side.
    if not reason and any(s.get("unpriced_calls") for s in sides.values()):
        reason = "unpriced"
    return {"id": meta["id"], "order": order, "sides": sides, "inconclusive": bool(reason), "reason": reason}
