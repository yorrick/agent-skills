"""A blind judge: Codex compares the two results without knowing which side made which."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import copy_tree, refuse_if_ignored_leaked, refuse_if_unusable, restored_ignored, run

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

Both started from the same state, saved as the git ref refs/jev/start in each folder, so
`git -C A add -A && git -C A diff --cached refs/jev/start` shows exactly what the first agent changed,
new files included (the same for B; these folders are copies, so staging in them is fine).

For each folder: run the project's tests if it has any, and decide whether the job's stated outcome
is met. Then say which result is better, or tie. Change nothing except test caches.

End your reply with one line of JSON, shaped like this example, and nothing after it:
{example}
"""


def run_codex(prompt: str, work: Path) -> str:
    out = work / "verdict.md"
    subprocess.run(
        [
            "codex",
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
        ],
        input=prompt,
        text=True,
        capture_output=True,
        timeout=2 * 3600,
        check=True,
    )
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
    """The last line that is a complete, well-formed verdict; anything else is an error,
    never a guess (a string "false" is not a boolean, an unknown preference is not a tie)."""
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _valid(data):
            return data
    raise ValueError("the judge gave no well-formed verdict line")


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
        copy_tree(Path(result["sides"][side]["clone"]), work / letter)
        # Reason: the origin and the reflogs record which replay folder this came
        # from, which result.json maps to a side; the judge gets neither.
        run("git", "-C", str(work / letter), "remote", "remove", "origin")
        shutil.rmtree(work / letter / ".git" / "logs", ignore_errors=True)
    raw = parse(codex(PROMPT.format(message=(snap / "message.txt").read_text(), example=EXAMPLE), work))
    verdict = {
        names["A"]: {"tests": raw["A"]["tests"], "outcome_met": raw["A"]["outcome_met"]},
        names["B"]: {"tests": raw["B"]["tests"], "outcome_met": raw["B"]["outcome_met"]},
        "prefer": names.get(raw["prefer"], "tie"),
        "why": str(raw.get("why", "")),
    }
    (work / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict
