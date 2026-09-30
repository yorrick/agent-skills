"""A blind judge: Codex compares the two results without knowing which side made which."""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import copy_for_judge, refuse_if_ignored_leaked, refuse_if_unusable, restored_ignored, run

EXAMPLE = json.dumps(
    {
        "A": {"tests": "pass|fail|none", "outcome_met": True},
        "B": {"tests": "pass|fail|none", "outcome_met": True},
        "prefer": "A|B|tie",
        "why": "one sentence",
    }
)
PROMPT = """You are a LEAF reviewer: do not invoke any other AI CLI (no claude, codex, opencode, gemini).

Two AI coding agents were given the same job in copies of the same repository: folder A and folder B.
You do not know which agent made which, and the order is random. Read only inside folders A and B; the
mapping between them and the two agents lives outside those folders, out of your reach. The job:

<job>
{message}
</job>

Any path in the job that names the user's own checkout corresponds to the root of A, and equally to the
root of B.

Both started from the same state, saved as the git ref refs/jev/start in each folder, so
`git -C A add -A && git -C A diff --cached refs/jev/start` shows exactly what the first agent changed,
new files included (the same for B; these folders are copies, so staging in them is fine).

For each folder: run the project's tests if it has any, and decide whether the job's stated outcome
is met. Then say which result is better, or tie. Change nothing except test caches.

End your reply with one line of JSON, shaped like this example, and nothing after it:
{example}
"""


TIMEOUT_SECONDS = 2 * 3600


def run_codex(prompt: str, work: Path) -> str:
    """One blind Codex run in `work`. `JEV_FORK_CHECK_CODEX` names another
    executable, for tests. On a failure, the error carries an excerpt of
    codex's stderr, which is what explains it."""
    out = work / "verdict.md"
    cmd = [
        os.environ.get("JEV_FORK_CHECK_CODEX", "codex"),
        "exec",
        "-m",
        "gpt-6-sol",
        "-c",
        "model_reasoning_effort=max",
        "--disable",
        "hooks",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-C",
        str(work),
        "-o",
        str(out),
        "-",
    ]
    # Reason: a new session, so a timeout kills codex together with the test
    # runs and servers it started, the way a timed-out replay is killed.
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(prompt, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise RuntimeError(f"codex exec timed out after {TIMEOUT_SECONDS} s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"codex exec failed: {(stderr or '').strip()[:500]}")
    return out.read_text()


def _valid(data: object) -> bool:
    if not isinstance(data, dict) or data.get("prefer") not in ("A", "B", "tie"):
        return False
    return all(
        isinstance(data.get(k), dict)
        and data[k].get("tests") in ("pass", "fail", "none")
        and isinstance(data[k].get("outcome_met"), bool)
        for k in ("A", "B")
    )


def parse(text: str) -> dict:
    """The last line that parses as JSON at all, which must be a complete,
    well-formed verdict or this raises: a line that is not JSON (prose,
    reasoning) is skipped looking for it, but once found, it is final. Ruling
    T13d: an earlier, valid-looking line is never used to paper over a later,
    malformed one, since that would let a draft verdict stand in for the
    judge's actual last word."""
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _valid(data):
            raise ValueError("the judge's last verdict line is not well-formed")
        return data
    raise ValueError("the judge gave no verdict line")


def _drop_unneeded_objects(copy: Path) -> None:
    """Keep in the copy's `.git` only what the judge reads: HEAD's history,
    refs/jev/start, and an index of the files the copy holds (Rulings F24,
    F26). A stash (`git add -f .env && git stash`), a blob staged and then
    reset, deleted or edited, or a side branch the replay committed `.env` to
    would otherwise still hold the secret. Every other ref and pseudo-ref
    (`ORIG_HEAD` and the like) goes, the index is rebuilt from HEAD and the
    copy's own files, then `git gc --prune=now` drops every object nothing
    reachable holds; the reflogs are already gone."""
    git_dir = copy / ".git"
    for name in os.listdir(git_dir):
        if name.endswith("_HEAD") or name == "AUTO_MERGE":
            (git_dir / name).unlink(missing_ok=True)
    # Reason: gc keeps every blob the index names, extensions such as
    # resolve-undo included, so the old index is removed rather than updated.
    # A lock a killed replay left behind would stop the rebuild, and there is
    # no other writer in a copy.
    for name in ("index", "index.lock"):
        (git_dir / name).unlink(missing_ok=True)
    run("git", "-C", str(copy), "read-tree", "HEAD")
    run("git", "-C", str(copy), "add", "-A")
    head = subprocess.run(["git", "-C", str(copy), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True)
    keep = {"refs/jev/start", head.stdout.strip()}
    refs = run("git", "-C", str(copy), "for-each-ref", "--format=%(refname)").split()
    doomed = [ref for ref in refs if ref not in keep]
    if doomed:
        subprocess.run(
            ["git", "-C", str(copy), "update-ref", "--stdin"],
            input="".join(f"delete {ref}\n" for ref in doomed),
            text=True,
            capture_output=True,
            check=True,
        )
    run("git", "-C", str(copy), "gc", "--prune=now", "--quiet")


def _refuse_if_source_path_leaked(git_dir: Path, source: Path, label: str) -> None:
    """Defense in depth, after the origin, the reflogs and FETCH_HEAD are all
    stripped: confirm no file left under `git_dir` still spells out `source`'s
    own path, before the judge is ever shown this copy. None of the three
    removals above would catch a linked worktree's `.git/worktrees/*/gitdir`,
    which points straight back at the source clone by path."""
    needle = str(source).encode()
    for f in git_dir.rglob("*"):
        if f.is_file() and needle in f.read_bytes():
            raise RuntimeError(f"{label}'s copy still names its source clone, in {f}; refusing")


def _refuse_links_into_the_runner(copy: Path, fork_root: Path, label: str) -> None:
    """A replay runs with full access and can leave a link such as
    `sides -> <fork root>/results/<id>/result.json`, and following it would tell
    the judge which side is which. Refuses any symlink in the blind copy that
    resolves inside the fork-check folder but outside the copy itself. Links
    that point elsewhere stay: a uv venv's `python` is an absolute link to the
    interpreter. A link loop leads nowhere and is left alone."""
    root, own = fork_root.resolve(), copy.resolve()
    for dirpath, dirnames, filenames in os.walk(copy):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            try:
                target = path.resolve()
            except (RuntimeError, OSError):
                continue
            if target.is_relative_to(root) and not target.is_relative_to(own):
                raise RuntimeError(
                    f"{label}'s copy holds {path.relative_to(copy)}, a link into the fork-check folder; refusing"
                )


def judge(
    snap: Path, result: dict, work: Path, *, codex: Callable[[str, Path], str] = run_codex, rng: random.Random
) -> dict:
    # Ruling T13a: refuse before anything is copied or spent on codex, exactly
    # like publish: an inconclusive result or a side with no priced cost is
    # nothing to judge, and a clone that still holds a restored ignored path
    # (committed, in its history since refs/jev/start, or merely sitting in
    # its working tree right now, since the judge never commits anything)
    # must never be shown to the judge.
    refuse_if_unusable(result)
    for side in ("keep", "delegate"):
        clone = Path(result["sides"][side]["clone"])
        refuse_if_ignored_leaked(clone, restored_ignored(clone), side)
    labels = ["keep", "delegate"]
    rng.shuffle(labels)
    names = dict(zip(("A", "B"), labels, strict=True))
    work.mkdir(parents=True, exist_ok=True)
    for letter, side in names.items():
        source = Path(result["sides"][side]["clone"])
        # Reason: the judge is a model; no secret ever reaches it (copy_for_judge
        # says what is left out), while dependency folders stay so tests can run.
        copy_for_judge(source, work / letter)
        git_dir = work / letter / ".git"
        # Reason: the origin, the reflogs and FETCH_HEAD all record which
        # replay folder this came from, which result.json maps to a side; the
        # judge gets none of them.
        run("git", "-C", str(work / letter), "remote", "remove", "origin")
        shutil.rmtree(git_dir / "logs", ignore_errors=True)
        (git_dir / "FETCH_HEAD").unlink(missing_ok=True)
        _drop_unneeded_objects(work / letter)
        _refuse_if_source_path_leaked(git_dir, source, letter)
        # Reason: the snapshot lives at <fork root>/snapshots/<id>.
        _refuse_links_into_the_runner(work / letter, snap.parent.parent, letter)
    raw = parse(codex(PROMPT.format(message=(snap / "message.txt").read_text(), example=EXAMPLE), work))
    verdict = {
        names["A"]: {"tests": raw["A"]["tests"], "outcome_met": raw["A"]["outcome_met"]},
        names["B"]: {"tests": raw["B"]["tests"], "outcome_met": raw["B"]["outcome_met"]},
        "prefer": names.get(raw["prefer"], "tie"),
        "why": str(raw.get("why", "")),
    }
    (work / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict
