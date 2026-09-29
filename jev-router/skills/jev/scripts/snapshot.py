"""Capture what a fork-check replay needs, from inside the hook, before the turn runs.

The turn starts editing files seconds after the hook returns, so everything here is
fast: a transcript copy, `git diff`, a tar of untracked files, and a cheap
fingerprint of the ignored setup files and dependencies (sizes and modification
times, never their contents except small `.env` files).

Every git call and the tar loop respect an optional deadline (a `time.monotonic()`
value), so a snapshot that is about to run out of time fails fast instead of
running past the hook's own timeout. The snapshot is built in a `.tmp` sibling
folder and only renamed into place once everything, including `meta.json`, is
written, so a hook killed mid-snapshot never leaves a half-written snapshot.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

# Reason: sibling module, found because `uv run --script` puts this folder first on sys.path.
import usage

MAX_UNTRACKED_BYTES = 50_000_000
# Reason: package managers touch these when packages come and go (site-packages'
# own modification time changes when a package directory is added or removed).
# Cheap by design: a change inside one package that leaves them alone goes unseen.
DEPENDENCY_DIRS = {
    "node_modules": (".package-lock.json", ".modules.yaml", ".yarn-state.yml"),
    ".venv": ("pyvenv.cfg", "lib/python*/site-packages"),
    "venv": ("pyvenv.cfg", "lib/python*/site-packages"),
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


def git(cwd: Path, *args: str, deadline: float | None = None) -> str:
    return _git_bytes(cwd, *args, deadline=deadline).decode()


def _git_bytes(cwd: Path, *args: str, deadline: float | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=_time_left(deadline))
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


def ignored_fingerprint(top: Path, deadline: float | None = None) -> str:
    """Changes when installed dependencies or `.env` files change, not when caches do."""
    listing = _git_bytes(
        top, "status", "--ignored", "--porcelain=v1", "-z", "--untracked-files=normal", deadline=deadline
    )
    ignored = sorted(e[3:].decode().rstrip("/") for e in listing.split(b"\0") if e.startswith(b"!! "))
    digest = hashlib.sha256()
    for rel in ignored:
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
        untracked = [
            p
            for p in _git_bytes(top, "ls-files", "--others", "--exclude-standard", "-z", deadline=deadline).split(b"\0")
            if p
        ]
        if sum((top / p.decode()).stat().st_size for p in untracked) > MAX_UNTRACKED_BYTES:
            raise SnapshotError("untracked files are too large to snapshot")
        tmp.mkdir(parents=True)
        transcript = Path(payload["transcript_path"])
        (tmp / "transcript.jsonl").write_bytes(trim_before_prompt(transcript.read_bytes(), prompt))
        (tmp / "message.txt").write_text(prompt)
        (tmp / "note.txt").write_text(note)
        (tmp / "changes.diff").write_bytes(_git_bytes(top, "diff", "--binary", "HEAD", deadline=deadline))
        with tarfile.open(tmp / "untracked.tar", "w") as tar:
            for p in untracked:
                if deadline is not None and time.monotonic() >= deadline:
                    raise SnapshotError("snapshot ran out of time")
                tar.add(top / p.decode(), arcname=p.decode())
        meta = {
            "id": sid,
            "created": now.isoformat(timespec="seconds"),
            "session_id": payload["session_id"],
            "transcript_path": str(transcript),
            "cwd": str(cwd),
            "toplevel": str(top),
            "head": head,
            "branch": branch,
            "ignored_fingerprint": ignored_fingerprint(top, deadline=deadline),
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
