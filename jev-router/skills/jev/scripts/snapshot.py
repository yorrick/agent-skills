"""Capture what a fork-check replay needs, from inside the hook, before the turn runs.

The turn starts editing files seconds after the hook returns, so everything here is
fast: a transcript copy, `git diff`, a tar of untracked files, and a cheap
fingerprint of the ignored setup files and dependencies (sizes and modification
times, never their contents except small `.env` files).

Only regular files and symlinks go into the tar; anything else `git ls-files
--others` reports (chiefly a nested repository or worktree, which comes back as
one directory entry) is skipped and named in `meta.json["skipped"]` instead, so a
snapshot never walks another repository's whole tree.

Every git call and the tar loop respect an optional deadline (a `time.monotonic()`
value), so a snapshot that is about to run out of time fails fast instead of
running past the hook's own timeout. The snapshot is built in a `.tmp` sibling
folder and only renamed into place once everything, including `meta.json`, is
written, so a hook killed mid-snapshot never leaves a half-written snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

# Reason: sibling module, found because `uv run --script` puts this folder first on sys.path.
import usage

MAX_UNTRACKED_BYTES = 50_000_000
# Reason: package managers touch these when packages come and go. Cheap by design:
# a change inside one package that leaves them alone goes unseen. Python
# virtualenvs are not here: a replay never copies one (`uv run` rebuilds it in the
# clone), so a change to the real one must not make a job inconclusive.
DEPENDENCY_DIRS = {
    "node_modules": (".package-lock.json", ".modules.yaml", ".yarn-state.yml"),
}


class SnapshotError(RuntimeError):
    pass


def _time_left(deadline: float | None) -> float:
    """Seconds left for the next git call: 10 with no deadline, otherwise whatever
    is left, capped at 10. Raises once nothing is left, before running git."""
    if deadline is None:
        return 10
    left = deadline - time.monotonic()
    if left <= 0:
        raise SnapshotError("snapshot ran out of time")
    return min(10, left)


def git_env() -> dict[str, str]:
    """The environment of every git call on the user's real checkout. Without
    GIT_OPTIONAL_LOCKS=0, `git status` and `git diff` refresh the index and take
    `.git/index.lock` to save it, which fails a `git add` that the user's own
    session runs at the same moment."""
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def git(cwd: Path, *args: str, deadline: float | None = None) -> str:
    return _git_bytes(cwd, *args, deadline=deadline).decode()


def _git_bytes(cwd: Path, *args: str, deadline: float | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, timeout=_time_left(deadline), env=git_env()
        )
    except subprocess.TimeoutExpired:
        raise SnapshotError("snapshot ran out of time") from None
    if result.returncode != 0:
        raise SnapshotError(f"git {args[0]} failed")
    return result.stdout


def trim_before_prompt(data: bytes, prompt: str) -> bytes:
    """End the copy just before the message, whether or not Claude Code wrote it yet.

    The message is already written only if the last typed entry is this text and no
    model call follows it: an earlier identical message ("continue") has calls after it."""

    lines = data.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        try:
            entry = json.loads(lines[i])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            return data
        if usage.is_typed(entry):
            return b"".join(lines[:i]) if usage.entry_text(entry) == prompt.strip() else data
    return data


def ignored_entries(top: Path, *, deadline: float | None = None) -> list[str]:
    """Every path `git status` reports as ignored (files and directories both),
    relative to `top`. Shared by the snapshot's fingerprint and a replay's copy of
    ignored setup files, so the two never disagree on what "ignored" means."""
    listing = _git_bytes(
        top, "status", "--ignored", "--porcelain=v1", "-z", "--untracked-files=normal", deadline=deadline
    )
    return sorted(e[3:].decode().rstrip("/") for e in listing.split(b"\0") if e.startswith(b"!! "))


def ignored_fingerprint(top: Path, deadline: float | None = None, *, entries: list[str] | None = None) -> str:
    """Changes when installed dependencies or `.env` files change, not when caches do.

    `entries` are the ignored paths to look at, by default what git lists now. A
    restore passes the snapshot's own list instead: it copies only those, so an
    entry created after the capture must not change the answer."""
    digest = hashlib.sha256()
    for rel in ignored_entries(top, deadline=deadline) if entries is None else entries:
        path = top / rel
        name = path.name
        if name in DEPENDENCY_DIRS:
            for marker in (path, *(m for pattern in DEPENDENCY_DIRS[name] for m in sorted(path.glob(pattern)))):
                if marker.exists():
                    st = marker.stat()
                    digest.update(f"{marker.relative_to(top)}\t{st.st_size}\t{st.st_mtime_ns}\n".encode())
        elif name.startswith(".env") and path.is_file():
            digest.update(f"{rel}\t".encode() + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def take_snapshot(
    root: Path, payload: dict, prompt: str, note: str, event: dict, *, deadline: float | None = None
) -> str:
    """Build the snapshot under `<root>/snapshots/<id>.tmp` and rename it into place
    only once every file is written. Anything raised along the way removes the
    `.tmp` folder before propagating, so a partial snapshot never becomes visible."""
    cwd = Path(payload["cwd"])
    now = datetime.now(UTC)
    sid = f"{now:%Y%m%d-%H%M%S}-{str(payload['session_id'])[:8]}"
    tmp = root / "snapshots" / f"{sid}.tmp"
    final = root / "snapshots" / sid
    try:
        top = Path(git(cwd, "rev-parse", "--show-toplevel", deadline=deadline).strip())
        head = git(top, "rev-parse", "HEAD", deadline=deadline).strip()
        branch = git(top, "rev-parse", "--abbrev-ref", "HEAD", deadline=deadline).strip()
        raw = [
            p
            for p in _git_bytes(top, "ls-files", "--others", "--exclude-standard", "-z", deadline=deadline).split(b"\0")
            if p
        ]
        # Reason: a directory holding its own .git (a nested repo or a worktree)
        # comes back from `ls-files` as one entry, never descended into. Walking it
        # with tar.add would recurse through its whole tree, including its own
        # .git, in one uninterruptible call, and the user's worktrees live under
        # <repo>/.claude/worktrees/. Only regular files and symlinks are kept; every
        # other entry (a nested repo, a worktree, a socket, ...) is skipped and
        # named in meta.json instead.
        untracked: list[bytes] = []
        skipped: list[str] = []
        total = 0
        for p in raw:
            rel = p.decode()
            st = os.lstat(top / rel)
            if stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                untracked.append(p)
                total += st.st_size
            else:
                skipped.append(rel.rstrip("/"))
        if total > MAX_UNTRACKED_BYTES:
            raise SnapshotError("untracked files are too large to snapshot")
        tmp.mkdir(parents=True)
        transcript = Path(payload["transcript_path"])
        raw_transcript = transcript.read_bytes()
        trimmed_transcript = trim_before_prompt(raw_transcript, prompt)
        prompt_cut = trimmed_transcript != raw_transcript
        (tmp / "transcript.jsonl").write_bytes(trimmed_transcript)
        (tmp / "message.txt").write_text(prompt)
        (tmp / "note.txt").write_text(note)
        (tmp / "changes.diff").write_bytes(_git_bytes(top, "diff", "--binary", "HEAD", deadline=deadline))
        with tarfile.open(tmp / "untracked.tar", "w") as tar:
            for p in untracked:
                _time_left(deadline)  # raises once nothing is left
                tar.add(top / p.decode(), arcname=p.decode(), recursive=False)
        # Reason: restore copies only these entries, so a file ignored after the
        # capture (a new secret, a cache, the real turn's own build output) never
        # reaches a replay. Paths only, from the listing the fingerprint needs anyway.
        entries = ignored_entries(top, deadline=deadline)
        meta = {
            "id": sid,
            "created": now.isoformat(timespec="seconds"),
            "session_id": payload["session_id"],
            "transcript_path": str(transcript),
            "cwd": str(cwd),
            "toplevel": str(top),
            "head": head,
            "branch": branch,
            "ignored_fingerprint": ignored_fingerprint(top, entries=entries),
            "ignored_entries": entries,
            "skipped": skipped,
            "prompt_cut": prompt_cut,
            **{
                k: event.get(k)
                for k in ("context", "model", "helper", "expected_saving", "loss_probability", "median_calls")
            },
        }
        (tmp / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sid
