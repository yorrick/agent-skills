# jev-router 0.3 (Claude Code): cache-aware delegation, shadow mode and fork check

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In Claude Code, the router predicts how long a typed job will run, prices keeping it against handing it to a fresh subagent, logs the decision in shadow mode, snapshots the jobs it would delegate, and a fork-check runner replays each snapshot both ways to measure real cost, time and quality.

**Architecture:** The hook script `jev_router.py` keeps its 0.2.0 path for Codex and opencode and gains a cache-aware path for Claude Code, built from small stdlib-only sibling modules: `usage.py` (calls and prices from transcript entries), `session.py` (the transcript tail), `delegation.py` (calibration and the study's cost model) and `snapshot.py` (fork-check capture). A separate runner, `fork_check.py`, with `replay.py`, `publish.py`, `judge.py` and `report.py`, lists, checks, marks, replays, publishes, judges and reports. Codex moves to the cache-aware path in a later plan, after its own shadow calibration.

**Tech Stack:** Python 3.12 standard library only (the hook runs before every message), PEP 723 `uv run --script`, pytest, ruff, pyright, git, `gh`, `claude` CLI, `codex exec`.

**Spec:** `docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md` (branch `jev-cache-aware-spec`, commit `50ee2e0`). Visual review with the user's decisions D1 to D12: https://claude.ai/artifact/SqsVexTYQLzkrSsak6Pyq7

## Global Constraints

- The hook never gets in the way of a message: on any failure it prints nothing and exits 0.
- Hook modules use the standard library only; the fork-check scripts do too.
- The log keeps no message text and no string from Jev's response: only numbers, known size names, ids, paths and a truncated SHA-256 of the prompt.
- Codex and opencode keep their 0.2.0 behaviour unchanged in this plan (`hook codex`, `hook opencode`, `classify`).
- Jev call: model `typesafe/jev-1.13`, provider pinned `{"only": ["typesafe"], "allow_fallbacks": false}`, deadline 2 s, message capped at 4,000 characters, previous reply capped at 1,500 characters (the last 1,500), state keys `agent_previous_reply` then `message`.
- Steps buckets (Score, 0 to 4): "1 step: a direct answer from what is already known, no tools"; "2 to 3 steps: one quick lookup, command or small edit"; "4 to 10 steps: read or edit a few files, run a test"; "11 to 30 steps: a feature, a debugging session, or a change across several files"; "more than 30 steps: a long autonomous build, migration, review loop or investigation".
- Gates, frozen (decision D3): delegate only if expected saving >= $0.25 AND >= 15% of expected keep cost, AND probability that delegating costs more <= 20%.
- Cost model constants (the study's): helper context starts at 25,000 tokens; brief 2,000 output tokens; helper answer 2,000 tokens; relay output 800 tokens; a helper on a different model than the session is charged 1.25x its work; cache write 1h = 2x input price, 5m = 1.25x input price.
- Prices per million tokens (OpenRouter list, 2026-09-28): `claude-opus-5-5` in 4, out 20, read 0.2; `claude-sonnet-5` in 2, out 10, read 0.2; `claude-haiku-4-5-20251001` in 1, out 5, read 0.1.
- Modes in `config.json` `"mode"`: `shadow` (default: decide and log, tell the session nothing), `capture` (shadow plus a snapshot of every job it would delegate), `live` (tell the session to delegate). No separate tuning period (decision D4).
- `JEV_ROUTER=off` turns routing off in any run; `JEV_ROUTER=on` opts a headless run in; `JEV_ROUTER_NOTE_FILE=<path>` makes a Claude hook print that note verbatim and nothing else (fork-check replays only).
- Replays run with the same access as the user's session (bypass permissions, same MCP servers, network, credentials) (D5); each clone's `origin` is a local bare copy (D8); two replays per job, random order (D7); 20 marked jobs, taken in capture order (D6, D10).
- Results publish to a private GitHub copy named `<owner>/<repo>-jev-replays`, never a fork; published commits never include ignored files.
- The blind judge is Codex GPT-6 Sol: `codex exec -m gpt-6-sol -c model_reasoning_effort=max --disable hooks`.
- Fork-check data lives under `config["fork_check_dir"]` (set with `mode capture --dir`); the user's is `~/work/data/jev-fork-check`. Never under `/tmp`.
- No em dashes in any text a person reads (docs, notes, PR bodies, CLI output).
- Code blocks favour readability: run `uv run ruff format` on changed files (the repo's PostToolUse hook does it on every edit) and wrap any line `ruff check` still reports as too long.
- The judge runs before publishing, on untouched clones whose folder names, remotes and commits do not reveal which side is which.
- Every task ends green on: `uv run pytest tests/ -q`, `uv run ruff check scripts/ tests/ jev-router/`, `uv run ruff format --check .`, `uv run pyright`.

## Review Focus

- A session that just compacted: the context drops, so per-call deltas go negative; `added` must clip them at 0 and `context` must be the post-compaction size (Task 3 test `test_compaction_clips_negative_growth`).
- The first message of a brand-new session has no model call in the transcript yet: the router must have no opinion and log `outcome: "no_session"` without calling Jev (Task 6 test `test_first_message_of_a_session_has_no_opinion`).
- At UserPromptSubmit the transcript may or may not already hold the new message: the snapshot must end just before it either way (Task 7 tests `test_snapshot_cuts_the_prompt_if_already_written`, `test_snapshot_keeps_everything_if_prompt_not_written_yet` and `test_snapshot_keeps_an_earlier_identical_prompt`, for a repeated "continue").
- The conversation is full of the original checkout's absolute paths: a replay that reuses them would edit the user's real working copy, so the transcript copy is rewritten to the clone's path (Task 11 test `test_install_session_rewrites_paths_and_ids`).
- A replay side whose first call does not read the shared prefix from cache is not comparable: it is rerun in a fresh clone, and after 3 cold attempts the job is inconclusive, never scored (Task 11 test `test_cold_side_is_retried_then_inconclusive`).

## File Structure

| File | Responsibility |
|---|---|
| `jev-router/skills/jev/scripts/jev_router.py` (modify) | Hook entry, switches and modes; 0.2.0 path for Codex/opencode; cache-aware path for Claude |
| `jev-router/skills/jev/scripts/usage.py` (create) | Transcript entries to priced model calls; typed-input detection |
| `jev-router/skills/jev/scripts/session.py` (create) | Read a transcript tail into a `Session` |
| `jev-router/skills/jev/scripts/delegation.py` (create) | Calibration table and the keep/delegate cost model and gates |
| `jev-router/skills/jev/scripts/snapshot.py` (create) | Capture a fork-check snapshot from the hook |
| `jev-router/skills/jev/scripts/prices.json`, `calibration.json` (create) | Data for the cost model |
| `jev-router/skills/jev/scripts/fork_check.py` (create) | Runner CLI: `list`, `check`, `mark`, `replay`, `publish`, `judge`, `report`, `shadow` |
| `jev-router/skills/jev/scripts/replay.py` (create) | Restore a snapshot into clones, run both sides, measure |
| `jev-router/skills/jev/scripts/publish.py` (create) | Push results to the private copy and open the comparison PR |
| `jev-router/skills/jev/scripts/judge.py` (create) | Blind Codex judge |
| `jev-router/skills/jev/scripts/report.py` (create) | Shadow report and fork-check report |
| `scripts/fit_jev_calibration.py` (create) | Dev tool: fit `calibration.json` from the study data |
| `jev-router/agents/*.md` (modify) | Helpers never commit, push, open PRs or deploy |
| `tests/test_jev_*.py`, `tests/fake_claude.py` (create) | Tests and a fake `claude` binary |
| `pyproject.toml` (modify) | pyright `extraPaths` gains the scripts folder |

---

### Task 1: Headless opt-in, opt-out and the replay note

**Files:**
- Modify: `jev-router/skills/jev/scripts/jev_router.py` (`route`, `hook_output`)
- Test: `tests/test_jev_router.py`

**Interfaces:**
- Produces: `hook_json(context: str) -> str` (the Claude/Codex UserPromptSubmit JSON); env contract `JEV_ROUTER` (`on`/`off`) and `JEV_ROUTER_NOTE_FILE`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_jev_router.py`; extend `run()` with an `env` parameter first)

```python
def run(
    home: Path, jev: FakeJev, *args: str, stdin: str = "", attended: str | None = "1", env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script as a harness would. `attended` is what Claude Code sets in
    CLAUDE_CODE_SESSION_ATTENDED: "1" in the TUI, "0" under `claude -p`."""
    full = {"JEV_ROUTER_HOME": str(home), "JEV_ROUTER_API_URL": jev.url, "PATH": "/usr/bin:/bin", **(env or {})}
    if attended is not None:
        full["CLAUDE_CODE_SESSION_ATTENDED"] = attended
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, env=full, capture_output=True, text=True, timeout=30
    )
```

Give `hook()` an `env: dict | None = None` parameter passed through to `run()`, then add:

```python
def test_jev_router_off_wins_even_when_switched_on(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    assert hook(home, jev, harness="codex", env={"JEV_ROUTER": "off"}) == ""
    assert jev.requests == []


def test_jev_router_on_routes_a_headless_run(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    out = hook(home, jev, harness="codex", source="exec", env={"JEV_ROUTER": "on"})
    assert "spawn_agent" in out
    assert len(jev.requests) == 1


def test_note_file_is_printed_verbatim_without_asking_jev(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    note = tmp_path / "note.txt"
    note.write_text("Jev router: hand this to the large subagent.")
    out = hook(home, jev, harness="claude", attended="0", env={"JEV_ROUTER": "off", "JEV_ROUTER_NOTE_FILE": str(note)})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "Jev router: hand this to the large subagent."
    assert jev.requests == []
    assert log(home) == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_router.py -q -k "jev_router_off or jev_router_on or note_file"`
Expected: 3 failures (the env var is ignored; the note is not printed).

- [ ] **Step 3: Implement**

In `jev_router.py`, add after `hook_output`:

```python
def hook_json(context: str) -> str:
    """What Claude Code and Codex read from a UserPromptSubmit hook."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})
```

and make the last line of `hook_output` `return hook_json(context)`. Replace the first lines of `route()` up to `prompt = ...` with:

```python
def route(harness: str, stdin: str) -> str:
    """What the hook prints: a hand-off, or '' for "carry on as if I wasn't here"."""
    # Reason: a fork-check replay must see exactly the note the live router gave
    # when the job was captured, and nothing else, so it never asks Jev again.
    note_file = os.environ.get("JEV_ROUTER_NOTE_FILE")
    if note_file:
        return hook_json(Path(note_file).read_text()) if harness == "claude" else ""
    if os.environ.get("JEV_ROUTER") == "off":
        return ""
    config = load_config()
    if not config.get("enabled"):
        return ""
    payload = json.loads(stdin)
    # Reason: headless runs are reviews and automation, unrouted unless a person
    # opts one in, such as an eval (`JEV_ROUTER=on claude -p ...`).
    if os.environ.get("JEV_ROUTER") != "on" and not interactive(harness, payload):
        return ""
    prompt = str(payload.get("prompt") or "").strip()
```

- [ ] **Step 4: Run the whole file**

Run: `uv run pytest tests/test_jev_router.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jev-router/skills/jev/scripts/jev_router.py tests/test_jev_router.py
git commit -m "jev-router: JEV_ROUTER on/off and the replay note file"
```

---

### Task 2: Calls and prices from transcript entries (`usage.py`)

**Files:**
- Create: `jev-router/skills/jev/scripts/usage.py`, `jev-router/skills/jev/scripts/prices.json`
- Modify: `pyproject.toml` (`[tool.pyright] extraPaths`), `tests/test_jev_router.py` (sys.path before loading the script)
- Test: `tests/test_jev_usage.py`

**Interfaces:**
- Produces: `Prices(inp, out, read, w1h, w5m)` in dollars per token; `load_prices(path: Path | None = None) -> dict[str, Prices]`; `canonical_model(model: str) -> str`; `Call(model, inp, read, w1h, w5m, out)` with `.context -> int` and `.cost(prices: dict[str, Prices]) -> float | None`; `calls(entries: list[dict], *, sidechain: bool = False) -> list[Call]` (one per request, with the largest output count seen for it, as the study's extractor does); `entry_text(entry: dict) -> str | None`; `is_typed(entry: dict) -> bool`; `read_entries(path: Path) -> list[dict]`; `assistant_text(entry: dict) -> str | None`.

- [ ] **Step 1: Write `prices.json`**

```json
{
  "source": "OpenRouter list prices, 2026-09-28; cache writes are derived: 1 hour = 2x input, 5 minutes = 1.25x input",
  "per_million_tokens": {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "read": 0.2},
    "claude-sonnet-5": {"in": 2.0, "out": 10.0, "read": 0.2},
    "claude-haiku-4-5-20251001": {"in": 1.0, "out": 5.0, "read": 0.1}
  }
}
```

- [ ] **Step 2: Write the failing tests** (`tests/test_jev_usage.py`)

```python
"""Calls and prices read from Claude Code transcript entries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import usage  # noqa: E402


def assistant(request: str, *, model: str = "claude-opus-5-5", inp: int = 10, read: int = 1000, w1h: int = 50,
              w5m: int = 0, out: int = 20, sidechain: bool = False, text: str | None = None) -> dict:
    content = [{"type": "text", "text": text}] if text else [{"type": "tool_use", "name": "Read", "input": {}}]
    return {
        "type": "assistant", "isSidechain": sidechain, "requestId": request,
        "message": {"id": f"msg-{request}", "model": model, "content": content, "usage": {
            "input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": w1h + w5m,
            "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m},
            "output_tokens": out}},
    }


def typed(text: str) -> dict:
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def test_one_call_per_request_with_its_largest_output() -> None:
    entries = [assistant("r1", out=5), assistant("r1", out=99), assistant("r2")]
    got = usage.calls(entries)
    assert [c.out for c in got] == [99, 20]


def test_synthetic_and_sidechain_entries_are_not_main_calls() -> None:
    entries = [assistant("r1", model="<synthetic>"), assistant("r2", sidechain=True), assistant("r3")]
    assert len(usage.calls(entries)) == 1
    assert len(usage.calls(entries, sidechain=True)) == 1


def test_context_adds_all_input_categories() -> None:
    (call,) = usage.calls([assistant("r1", inp=1, read=2, w1h=3, w5m=4)])
    assert call.context == 10


def test_missing_split_counts_every_cache_write_as_one_hour() -> None:
    entry = assistant("r1")
    del entry["message"]["usage"]["cache_creation"]
    (call,) = usage.calls([entry])
    assert (call.w1h, call.w5m) == (50, 0)


def test_price_of_an_opus_call() -> None:
    prices = usage.load_prices()
    call = usage.Call(model="claude-opus-5-5", inp=1_000, read=100_000, w1h=2_000, w5m=0, out=500)
    # 1k*4 + 100k*0.2 + 2k*8 + 500*20, per million
    assert call.cost(prices) == pytest.approx((4_000 + 20_000 + 16_000 + 10_000) / 1e6)


def test_unknown_model_has_no_price() -> None:
    call = usage.Call(model="gpt-6-sol", inp=1, read=1, w1h=1, w5m=1, out=1)
    assert call.cost(usage.load_prices()) is None


def test_context_window_suffix_is_not_part_of_the_model() -> None:
    assert usage.canonical_model("claude-opus-5-5[1m]") == "claude-opus-5-5"


def test_typed_input_and_the_rest() -> None:
    assert usage.is_typed(typed("fix the bug"))
    tool_result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    assert not usage.is_typed(tool_result)
    assert not usage.is_typed({**typed("<command-name>/jev</command-name>")})
    assert not usage.is_typed({"type": "user", "isMeta": True, "message": {"content": "meta"}})
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_jev_usage.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'usage'`.

- [ ] **Step 4: Implement `usage.py`**

```python
"""Model calls and their price, read from Claude Code transcript entries.

Shared by the hook, the shadow report and the fork check, so all three count and
price calls exactly as the study did (docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Prices:
    """US dollars per token."""

    inp: float
    out: float
    read: float
    w1h: float
    w5m: float


def load_prices(path: Path | None = None) -> dict[str, Prices]:
    data = json.loads((path or Path(__file__).with_name("prices.json")).read_text())
    return {
        model: Prices(
            inp=p["in"] / 1e6, out=p["out"] / 1e6, read=p["read"] / 1e6, w1h=2 * p["in"] / 1e6, w5m=1.25 * p["in"] / 1e6
        )
        for model, p in data["per_million_tokens"].items()
    }


def canonical_model(model: str) -> str:
    """'claude-opus-5-5[1m]' -> 'claude-opus-5-5': the suffix names a context window, not a price."""
    return re.sub(r"\[.*\]$", "", model)


@dataclass(frozen=True)
class Call:
    model: str
    inp: int
    read: int
    w1h: int
    w5m: int
    out: int

    @property
    def context(self) -> int:
        """Every input token the call sent, cached or not."""
        return self.inp + self.read + self.w1h + self.w5m

    def cost(self, prices: dict[str, Prices]) -> float | None:
        p = prices.get(self.model)
        if p is None:
            return None
        return self.inp * p.inp + self.read * p.read + self.w1h * p.w1h + self.w5m * p.w5m + self.out * p.out


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def calls(entries: list[dict], *, sidechain: bool = False) -> list[Call]:
    """One Call per model request. Claude Code writes one entry per content block,
    each carrying the request's usage; the inputs come from the first and the output
    is the largest seen, exactly as the study's extractor counts them.
    `<synthetic>` entries are Claude Code's own (an error or an interrupt), not calls."""
    seen: dict[str, int] = {}
    found: list[Call] = []
    for entry in entries:
        if entry.get("type") != "assistant" or bool(entry.get("isSidechain")) != sidechain:
            continue
        message = entry.get("message") or {}
        usage = message.get("usage")
        request = entry.get("requestId") or message.get("id")
        model = str(message.get("model") or "?")
        if not isinstance(usage, dict) or not request or model == "<synthetic>":
            continue
        out = _int(usage.get("output_tokens"))
        if request in seen:
            i = seen[request]
            if out > found[i].out:
                found[i] = replace(found[i], out=out)
            continue
        seen[request] = len(found)
        created = _int(usage.get("cache_creation_input_tokens"))
        split = usage.get("cache_creation")
        w5m = _int(split.get("ephemeral_5m_input_tokens")) if isinstance(split, dict) else 0
        found.append(
            Call(
                model=canonical_model(model),
                inp=_int(usage.get("input_tokens")),
                read=_int(usage.get("cache_read_input_tokens")),
                w1h=max(0, created - w5m),
                w5m=w5m,
                out=out,
            )
        )
    return found


def entry_text(entry: dict) -> str | None:
    """The text a user entry carries, or None for a tool result."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    return None


def is_typed(entry: dict) -> bool:
    """Whether a person typed this entry (the study's rule): Claude Code marks a
    person's input with origin.kind "human"; older transcripts have no origin."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    text = entry_text(entry)
    if not text or text.startswith(("<command-name>", "<command-message>", "<bash-input>")):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict):
        return origin.get("kind") == "human"
    return not text.startswith("<")


def assistant_text(entry: dict) -> str | None:
    """The last text block of a main-thread assistant entry, if any."""
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return None
    texts = [
        b["text"]
        for b in (entry.get("message") or {}).get("content") or []
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
    ]
    return texts[-1] if texts else None


def read_entries(path: Path) -> list[dict]:
    entries = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries
```

- [ ] **Step 5: Let pyright and the router tests find the sibling modules**

In `pyproject.toml`: `extraPaths = ["dev-loop/scripts", "jev-router/skills/jev/scripts"]`. In `tests/test_jev_router.py`, before `_spec = importlib.util.spec_from_file_location(...)`, add `sys.path.insert(0, str(SCRIPT.parent))`.

- [ ] **Step 6: Run**

Run: `uv run pytest tests/test_jev_usage.py tests/test_jev_router.py -q && uv run pyright`
Expected: all pass, 0 pyright errors.

- [ ] **Step 7: Commit**

```bash
git add jev-router/skills/jev/scripts/usage.py jev-router/skills/jev/scripts/prices.json tests/test_jev_usage.py tests/test_jev_router.py pyproject.toml
git commit -m "jev-router: price model calls from transcript entries"
```

---

### Task 3: Read the session from the transcript tail (`session.py`)

**Files:**
- Create: `jev-router/skills/jev/scripts/session.py`
- Test: `tests/test_jev_session.py`

**Interfaces:**
- Consumes: `usage.calls`, `usage.assistant_text`, `usage.canonical_model`.
- Produces: `Session(context: int, model: str, added: int, output: int, previous_reply: str)`; `read_session(path: Path, tail_bytes: int = TAIL_BYTES) -> Session | None`; constants `TAIL_BYTES = 4_000_000`, `WINDOW = 20`, `PREV_CHARS = 1500`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_session.py`)

```python
"""The session as the router sees it: read from the end of the transcript only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import session  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_context_growth_output_and_previous_reply(tmp_path: Path) -> None:
    entries = [
        typed("build it"),
        assistant("r1", read=100_000, w1h=2_000, out=300),
        assistant("r2", read=102_000, w1h=3_000, out=500),
        assistant("r3", read=105_000, w1h=1_000, out=100, text="Done. Tests pass."),
    ]
    s = session.read_session(write(tmp_path / "t.jsonl", entries))
    assert s is not None
    assert s.context == 10 + 105_000 + 1_000
    assert s.added == 2_000  # contexts 102010, 105010, 106010: growth (3000 + 1000) / 2
    assert s.output == 300
    assert s.model == "claude-opus-5-5"
    assert s.previous_reply == "Done. Tests pass."


def test_compaction_clips_negative_growth(tmp_path: Path) -> None:
    entries = [assistant("r1", read=500_000, w1h=0), assistant("r2", read=40_000, w1h=0), assistant("r3", read=42_000)]
    s = session.read_session(write(tmp_path / "t.jsonl", entries))
    assert s is not None
    assert s.context == 10 + 42_000 + 50
    assert s.added == (0 + 2_050) // 2


def test_only_the_last_twenty_calls_count(tmp_path: Path) -> None:
    early = [assistant(f"e{i}", read=1_000 * i, out=10_000) for i in range(30)]
    late = [assistant(f"l{i}", read=100_000, out=100) for i in range(20)]
    s = session.read_session(write(tmp_path / "t.jsonl", early + late))
    assert s is not None
    assert s.output == 100


def test_previous_reply_keeps_the_last_1500_characters(tmp_path: Path) -> None:
    s = session.read_session(write(tmp_path / "t.jsonl", [assistant("r1", text="x" * 1000 + "y" * 1500)]))
    assert s is not None
    assert s.previous_reply == "y" * 1500


def test_a_session_with_no_call_has_no_reading(tmp_path: Path) -> None:
    assert session.read_session(write(tmp_path / "t.jsonl", [typed("hello")])) is None


def test_missing_transcript_has_no_reading(tmp_path: Path) -> None:
    assert session.read_session(tmp_path / "nope.jsonl") is None


def test_only_the_tail_is_read(tmp_path: Path) -> None:
    padding = [typed("x" * 10_000) for _ in range(50)]
    path = write(tmp_path / "t.jsonl", [assistant("old", read=9)] + padding + [assistant("new", read=123)])
    s = session.read_session(path, tail_bytes=20_000)
    assert s is not None
    assert s.context == 10 + 123 + 50
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_session.py -q`
Expected: `ModuleNotFoundError: No module named 'session'`.

- [ ] **Step 3: Implement `session.py`**

```python
"""What the router needs from a Claude Code session, read from the transcript's end.

A long session's transcript runs to tens of megabytes, and the hook runs before
every message, so only the last few megabytes are read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import usage

TAIL_BYTES = 4_000_000
WINDOW = 20
PREV_CHARS = 1500


@dataclass(frozen=True)
class Session:
    context: int  # tokens the last main-thread call sent: what the next call re-reads
    model: str
    added: int  # new tokens per call, averaged over the last WINDOW calls
    output: int  # output tokens per call, averaged the same way
    previous_reply: str  # the agent's last text, its last PREV_CHARS characters


def _tail(path: Path, tail_bytes: int) -> list[dict]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - tail_bytes))
        lines = handle.read().split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # the first line was cut in the middle
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def read_session(path: Path, tail_bytes: int = TAIL_BYTES) -> Session | None:
    """None when there is nothing to go on: no transcript, or no model call yet."""
    try:
        entries = _tail(path, tail_bytes)
    except OSError:
        return None
    recent = usage.calls(entries)[-WINDOW:]
    if not recent:
        return None
    contexts = [c.context for c in recent]
    # Reason: a compaction shrinks the context; that is not negative growth.
    growth = [max(0, after - before) for before, after in zip(contexts, contexts[1:])]
    previous = ""
    for entry in entries:
        previous = usage.assistant_text(entry) or previous
    return Session(
        context=contexts[-1],
        model=recent[-1].model,
        added=sum(growth) // len(growth) if growth else 0,
        output=sum(c.out for c in recent) // len(recent),
        previous_reply=previous[-PREV_CHARS:],
    )
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_jev_session.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add jev-router/skills/jev/scripts/session.py tests/test_jev_session.py
git commit -m "jev-router: read the session from the transcript tail"
```

---

### Task 4: Calibration table

**Files:**
- Create: `scripts/fit_jev_calibration.py`, `jev-router/skills/jev/scripts/delegation.py` (loader only in this task), `jev-router/skills/jev/scripts/calibration.json`
- Test: `tests/test_jev_calibration.py`

**Interfaces:**
- Produces: `delegation.Bin(max_score: float | None, calls: tuple[int, ...])`; `delegation.load_calibration(path: Path | None = None) -> tuple[Bin, ...]`; `delegation.calls_for(bins: tuple[Bin, ...], score: float) -> tuple[int, ...]`; in the fit script `fit(pairs: list[tuple[float, int]], bins: int = 5) -> list[dict]` and `check(table: list[dict], pairs: list[tuple[float, int]]) -> list[str]`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_calibration.py`)

```python
"""Jev's step score mapped to the call counts really seen in the study."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delegation  # noqa: E402

_spec = importlib.util.spec_from_file_location("fit_jev_calibration", REPO / "scripts" / "fit_jev_calibration.py")
assert _spec and _spec.loader
fit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_mod)


def test_fit_makes_equal_count_bins_ordered_by_score() -> None:
    pairs = [(i / 10, i) for i in range(50)]
    table = fit_mod.fit(pairs, bins=5)
    assert [len(b["calls"]) for b in table] == [10] * 5
    assert [b["max_score"] for b in table] == [0.9, 1.9, 2.9, 3.9, None]
    assert table[0]["calls"] == list(range(10))


def test_calls_for_picks_the_first_bin_that_covers_the_score(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"bins": [{"max_score": 1.0, "calls": [1, 2]}, {"max_score": None, "calls": [30]}]}))
    bins = delegation.load_calibration(path)
    assert delegation.calls_for(bins, 0.4) == (1, 2)
    assert delegation.calls_for(bins, 1.0) == (1, 2)
    assert delegation.calls_for(bins, 3.9) == (30,)


def test_check_compares_long_job_shares_on_held_out_rows() -> None:
    table = [{"max_score": 1.0, "calls": [1, 1, 20, 20]}, {"max_score": None, "calls": [20]}]
    lines = fit_mod.check(table, [(0.5, 1), (0.5, 30), (2.0, 40)])
    assert lines[0] == "bin 0 (score <= 1.0): predicted 11+ 50%, held-out 50% of 2"
    assert lines[1] == "bin 1 (score > 1.0): predicted 11+ 100%, held-out 100% of 1"


def test_shipped_table_covers_every_score() -> None:
    bins = delegation.load_calibration()
    assert bins[-1].max_score is None
    assert all(b.calls for b in bins)
    assert delegation.calls_for(bins, 0.0) and delegation.calls_for(bins, 4.0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_calibration.py -q`
Expected: errors (no `delegation` module, no fit script).

- [ ] **Step 3: Implement the fit script** (`scripts/fit_jev_calibration.py`)

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Fit jev-router's calibration table from the study's labelled turns.

Usage: uv run scripts/fit_jev_calibration.py ~/work/data/jev-router-study/study/jev_steps.jsonl

Each labelled turn has Jev's step score, asked with the agent's previous reply
(the "context" answer), and the number of model calls the turn really took. Even
rows fit five equal-count bins of the score; odd rows are held out to check them.
The table keeps every observed call count, so the router can price the long tail
as it really is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BINS = 5
OUT = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts" / "calibration.json"


def pairs_from(path: Path) -> list[tuple[float, int]]:
    pairs = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        answer = row.get("context")
        if isinstance(answer, dict) and isinstance(answer.get("score"), int | float) and row.get("n_calls"):
            pairs.append((float(answer["score"]), int(row["n_calls"])))
    return pairs


def fit(pairs: list[tuple[float, int]], bins: int = BINS) -> list[dict]:
    ordered = sorted(pairs)
    size = len(ordered) / bins
    table = []
    for b in range(bins):
        chunk = ordered[round(b * size) : round((b + 1) * size)]
        table.append({"max_score": chunk[-1][0] if b < bins - 1 else None, "calls": sorted(n for _, n in chunk)})
    return table


def check(table: list[dict], pairs: list[tuple[float, int]]) -> list[str]:
    lines = []
    for i, b in enumerate(table):
        low = table[i - 1]["max_score"] if i else None
        high = b["max_score"]
        held = [n for s, n in pairs if (low is None or s > low) and (high is None or s <= high)]
        predicted = sum(n >= 11 for n in b["calls"]) / len(b["calls"])
        seen = sum(n >= 11 for n in held) / len(held) if held else 0.0
        where = f"score <= {high}" if high is not None else f"score > {low}"
        lines.append(f"bin {i} ({where}): predicted 11+ {predicted:.0%}, held-out {seen:.0%} of {len(held)}")
    return lines


def main(argv: list[str]) -> int:
    pairs = pairs_from(Path(argv[0]).expanduser())
    table = fit(pairs[0::2])
    for line in check(table, pairs[1::2]):
        print(line)
    OUT.write_text(
        json.dumps({"harness": "claude", "fitted_on": len(pairs[0::2]), "bins": table}, indent=1) + "\n"
    )
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Start `delegation.py` with the loader**

```python
"""Keep a job in the session, or hand it to a fresh subagent?

The calibration table turns Jev's step score into the call counts really seen in
the study; the cost model (Task 5) prices both options over those counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Bin:
    max_score: float | None  # None: every score above the previous bin
    calls: tuple[int, ...]


def load_calibration(path: Path | None = None) -> tuple[Bin, ...]:
    data = json.loads((path or Path(__file__).with_name("calibration.json")).read_text())
    return tuple(Bin(max_score=b["max_score"], calls=tuple(b["calls"])) for b in data["bins"])


def calls_for(bins: tuple[Bin, ...], score: float) -> tuple[int, ...]:
    for b in bins:
        if b.max_score is None or score <= b.max_score:
            return b.calls
    return bins[-1].calls
```

- [ ] **Step 5: Fit the shipped table from the study data**

Run: `uv run scripts/fit_jev_calibration.py ~/work/data/jev-router-study/study/jev_steps.jsonl`
Expected: five `bin` lines whose held-out shares sit near the predicted ones (at fit time: 7/10%, 16/10%, 19/12%, 29/28%, 66/67%), then `wrote .../calibration.json`.

- [ ] **Step 6: Run**

Run: `uv run pytest tests/test_jev_calibration.py -q`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add scripts/fit_jev_calibration.py jev-router/skills/jev/scripts/delegation.py jev-router/skills/jev/scripts/calibration.json tests/test_jev_calibration.py
git commit -m "jev-router: calibration table from the study's labelled turns"
```

---

### Task 5: The cost model and the gates

**Files:**
- Modify: `jev-router/skills/jev/scripts/delegation.py`
- Test: `tests/test_jev_delegation.py`

**Interfaces:**
- Consumes: `usage.Prices`.
- Produces: `keep_cost(k: int, start: int, add: int, out: int, p: Prices) -> float`; `delegate_cost(k, start, add, out, parent: Prices, helper: Prices, scale: float) -> float`; `Decision(delegate: bool, expected_keep: float, expected_saving: float, loss_probability: float, median_calls: int)`; `decide(calls: tuple[int, ...], start: int, add: int, out: int, parent: Prices, helper: Prices, same_model: bool) -> Decision`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_delegation.py`). The expected dollars are the study's own `costs.py` results for the same uniform jobs (checked when this plan was written).

```python
"""The study's keep/delegate cost model and the frozen gates."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delegation  # noqa: E402
import usage  # noqa: E402

OPUS = usage.load_prices()["claude-opus-5-5"]


@pytest.mark.parametrize(
    ("start", "k", "add", "out", "keep", "delegate"),
    [
        (500_000, 40, 1_500, 1_000, 5.514, 2.1856),
        (50_000, 3, 3_000, 2_000, 0.2238, 0.5112),
        (300_000, 5, 3_000, 2_000, 0.626, 0.7522),
        (1_000_000, 10, 3_000, 2_000, 2.667, 1.3952),
    ],
)
def test_matches_the_study(start: int, k: int, add: int, out: int, keep: float, delegate: float) -> None:
    assert delegation.keep_cost(k, start, add, out, OPUS) == pytest.approx(keep)
    assert delegation.delegate_cost(k, start, add, out, OPUS, OPUS, 1.0) == pytest.approx(delegate)


def test_long_jobs_in_a_big_session_are_delegated() -> None:
    d = delegation.decide((12, 20, 30, 40, 60), 800_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert d.delegate
    assert d.loss_probability == 0
    assert d.median_calls == 30


def test_short_jobs_are_kept() -> None:
    d = delegation.decide((1, 1, 2, 2, 3), 800_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert not d.delegate
    assert d.expected_saving < 0


def test_a_rare_very_long_job_does_not_carry_the_decision() -> None:
    d = delegation.decide((1,) * 8 + (200, 200), 300_000, 3_000, 1_500, OPUS, OPUS, same_model=True)
    assert d.expected_saving > 0.25
    assert d.expected_saving > 0.15 * d.expected_keep
    assert d.loss_probability == pytest.approx(0.8)
    assert not d.delegate


def test_another_model_is_charged_more_work() -> None:
    same = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, 1.0)
    other = delegation.delegate_cost(20, 300_000, 3_000, 1_500, OPUS, OPUS, delegation.OTHER_MODEL_SCALE)
    assert other > same
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_delegation.py -q`
Expected: `AttributeError: module 'delegation' has no attribute 'keep_cost'`.

- [ ] **Step 3: Implement** (append to `delegation.py`; add `from usage import Prices` to the imports)

```python
# The study's cost model (docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md),
# applied to a uniform job built from the session's recent averages. Keep: call i
# reads start + i*add from cache, writes `add` new tokens to the 1-hour cache and
# produces `out`. Delegate: a brief (the parent's next call, writing a brief
# instead of working), the helper's calls on a context that starts at 25k tokens
# and grows the same way, and a relay (one parent call that caches the brief and
# the helper's answer and produces a short reply).
HELPER_START = 25_000
BRIEF_OUT = 2_000
ANSWER = 2_000
RELAY_OUT = 800
# Reason: a helper on a different model is assumed to need 25% more calls, as the
# study assumed for Sonnet 5; the margins below absorb the rest.
OTHER_MODEL_SCALE = 1.25
# The user's frozen gates (decision D3).
MIN_SAVING_USD = 0.25
MIN_SAVING_SHARE = 0.15
MAX_LOSS_PROBABILITY = 0.20


def keep_cost(k: int, start: int, add: int, out: int, p: Prices) -> float:
    reads = k * start + add * k * (k - 1) // 2
    return reads * p.read + k * add * p.w1h + k * out * p.out


def delegate_cost(k: int, start: int, add: int, out: int, parent: Prices, helper: Prices, scale: float) -> float:
    brief = start * parent.read + add * parent.w1h + BRIEF_OUT * parent.out
    # Call 0 writes HELPER_START; call i >= 1 reads HELPER_START + (i-1)*add and writes `add`.
    helper_reads = (k - 1) * HELPER_START + add * (k - 2) * (k - 1) // 2
    helper_writes = HELPER_START + (k - 1) * add
    work = helper_reads * helper.read + helper_writes * helper.w1h + k * out * helper.out
    relay = (start + add) * parent.read + (BRIEF_OUT + ANSWER) * parent.w1h + RELAY_OUT * parent.out
    return brief + scale * work + relay


@dataclass(frozen=True)
class Decision:
    delegate: bool
    expected_keep: float
    expected_saving: float
    loss_probability: float
    median_calls: int


def decide(
    calls: tuple[int, ...], start: int, add: int, out: int, parent: Prices, helper: Prices, same_model: bool
) -> Decision:
    """Price both options over every call count the calibration saw for this score."""
    scale = 1.0 if same_model else OTHER_MODEL_SCALE
    keeps = [keep_cost(k, start, add, out, parent) for k in calls]
    savings = [
        keep - delegate_cost(k, start, add, out, parent, helper, scale) for k, keep in zip(calls, keeps, strict=True)
    ]
    expected_keep = sum(keeps) / len(keeps)
    expected_saving = sum(savings) / len(savings)
    loss = sum(s < 0 for s in savings) / len(savings)
    # Reason: the loss gate stops a rare very long job from making the average
    # positive while most jobs with this score would lose money.
    delegate = (
        expected_saving >= MIN_SAVING_USD
        and expected_saving >= MIN_SAVING_SHARE * expected_keep
        and loss <= MAX_LOSS_PROBABILITY
    )
    return Decision(delegate, expected_keep, expected_saving, loss, sorted(calls)[len(calls) // 2])
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_jev_delegation.py tests/test_jev_calibration.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jev-router/skills/jev/scripts/delegation.py tests/test_jev_delegation.py
git commit -m "jev-router: the study's cost model with the frozen gates"
```

---

### Task 6: The cache-aware path for Claude Code (shadow and live)

**Files:**
- Modify: `jev-router/skills/jev/scripts/jev_router.py`, `jev-router/agents/{tiny,everyday,large,hardest}.md`
- Test: `tests/test_jev_router.py`

**Interfaces:**
- Consumes: `session.read_session`, `delegation.load_calibration/calls_for/decide`, `usage.load_prices`, `hook_json` (Task 1).
- Produces: `steps_questions(tiers) -> dict`; `StepsVerdict(steps: float, size: str, probability: float)`; `parse_steps_verdict(body, tiers) -> StepsVerdict`; `delegate_note(tier: Tier, decision: delegation.Decision, context: int) -> str`; `route_cache_aware(config: dict, payload: dict, prompt: str) -> str`; `cmd_mode(mode: str, directory: str | None) -> int`; log event fields for version 3: `harness, version, mode, session_id, transcript_path, prompt_sha, outcome (no_session|timeout|error|unpriced|keep|delegate), context, model, added, output, latency_ms, answered, cost, steps, size, helper, helper_model, expected_keep, expected_saving, loss_probability, median_calls, calls` (the call counts the decision priced: numbers only); `ask_jev(..., state=None, questions=None)`.

- [ ] **Step 1: Point the 0.2.0 tests at Codex and add Claude helpers**

In `tests/test_jev_router.py`: change `hook()`'s default `harness` to `"codex"` (Codex keeps 0.2.0 behaviour, and the helper already writes an interactive Codex transcript). Delete `test_confident_job_is_handed_to_the_matching_claude_helper` and `test_claude_hands_off_unless_the_session_is_certain_it_matches`: Claude no longer gets 0.2.0 hand-offs. In the remaining 0.2.0 tests, change any assertion that names a Claude helper (`jev-router:<size>`, "subagent") to the Codex form of the same hand-off (`spawn_agent`, the tier's `model_id` and effort, as `test_codex_spawns_with_the_model_and_thinking_level` already does). In `test_headless_claude_is_never_routed_or_sent_to_jev`, pass `harness="claude"` explicitly (the helper's default is now Codex); it must still pass. Then add:

```python
from test_jev_usage import assistant, typed  # noqa: E402

CALIBRATION = {"bins": [{"max_score": 1.0, "calls": [1, 1, 2, 2, 3]}, {"max_score": None, "calls": [12, 20, 30, 40, 60]}]}


def steps_answers(score: float, size: str = "large", p: float = 0.8) -> dict:
    others = [s for s in ("tiny", "everyday", "large", "hardest") if s != size]
    return {
        "steps": {"type": "score", "score": score, "probabilities": dict.fromkeys("01234", 0.2)},
        "size": {"type": "choice", "choice": size, "confidence": 0.5,
                 "probabilities": {**dict.fromkeys(others, round((1 - p) / 3, 4)), size: p}},
    }


def claude_hook(home: Path, jev: FakeJev, *, prompt: str = "build the whole export feature",
                entries: list[dict] | None = None, env: dict | None = None) -> str:
    calibration = home.parent / "calibration.json"
    calibration.write_text(json.dumps(CALIBRATION))
    transcript = home.parent / "claude-transcript.jsonl"
    if entries is None:
        entries = [typed("start"), assistant("r1", read=800_000, w1h=3_000, out=1_500, text="Ready.")]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    payload = {"prompt": prompt, "session_id": "sess-1", "transcript_path": str(transcript),
               "cwd": str(home.parent), "permission_mode": "default"}
    result = run(home, jev, "hook", "claude", stdin=json.dumps(payload),
                 env={"JEV_ROUTER_CALIBRATION": str(calibration), **(env or {})})
    assert result.returncode == 0, result.stderr
    return result.stdout



def test_shadow_mode_decides_and_logs_but_tells_the_session_nothing(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.raw_answers = steps_answers(3.5)
    assert claude_hook(home, jev) == ""
    (event,) = log(home)
    assert event["version"] == 3 and event["mode"] == "shadow" and event["outcome"] == "delegate"
    assert event["expected_saving"] >= 0.25 and event["loss_probability"] == 0
    assert event["helper"] == "jev-router:large" and event["median_calls"] == 30
    assert len(event["prompt_sha"]) == 16
    assert "export feature" not in json.dumps(event)


def test_live_mode_tells_the_session_to_brief_a_fresh_subagent(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(3.5)
    note = json.loads(claude_hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert "`jev-router:large` subagent" in note
    assert "never the `fork` type" in note
    assert "not to commit, push, open pull requests or deploy" in note
    assert chr(0x2014) not in note


def test_short_jobs_stay_quiet_even_in_live_mode(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(0.2)
    assert claude_hook(home, jev) == ""
    assert log(home)[0]["outcome"] == "keep"


def test_follow_ups_are_decided_like_any_message(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(3.5)
    assert claude_hook(home, jev, prompt="ok go, iterate until the PR is ready") != ""


def test_first_message_of_a_session_has_no_opinion(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    assert claude_hook(home, jev, entries=[typed("hello")]) == ""
    assert log(home)[0]["outcome"] == "no_session"
    assert jev.requests == []


def test_jev_gets_the_steps_and_size_questions_with_the_previous_reply(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.raw_answers = steps_answers(3.5)
    claude_hook(home, jev)
    (request,) = jev.requests
    assert list(request["state"]) == ["agent_previous_reply", "message"]
    assert request["state"]["agent_previous_reply"] == "Ready."
    assert set(request["questions"]) == {"steps", "size"}
    assert request["questions"]["steps"]["type"] == "score"
    assert len(request["questions"]["steps"]["criteria"]) == 5
    assert request["provider"] == {"only": ["typesafe"], "allow_fallbacks": False}


def test_a_session_on_an_unpriced_model_is_not_decided(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(3.5)
    entries = [assistant("r1", model="claude-future-9", read=800_000)]
    assert claude_hook(home, jev, entries=entries) == ""
    assert log(home)[0]["outcome"] == "unpriced"


def test_a_step_score_out_of_range_is_an_error(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(5.0)
    assert claude_hook(home, jev) == ""
    assert log(home)[0]["outcome"] == "error"


def test_a_broken_calibration_file_never_blocks_the_message(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(3.5)
    bad = home.parent / "bad.json"
    bad.write_text("not json")
    assert claude_hook(home, jev, env={"JEV_ROUTER_CALIBRATION": str(bad)}) == ""
    assert "spawn_agent" in hook(home, jev, harness="codex", env={"JEV_ROUTER_CALIBRATION": str(bad)})


def test_shadow_log_keeps_the_priced_call_distribution(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.raw_answers = steps_answers(3.5)
    claude_hook(home, jev)
    assert log(home)[0]["calls"] == [12, 20, 30, 40, 60]


def test_mode_command_sets_the_mode_and_capture_dir(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    result = run(home, jev, "mode", "capture", "--dir", str(tmp_path / "fc"))
    assert result.returncode == 0
    config = json.loads((home / "config.json").read_text())
    assert config["mode"] == "capture" and config["fork_check_dir"] == str(tmp_path / "fc")
    assert "snapshot" in result.stdout
```

- [ ] **Step 2: Run to verify the new tests fail and the old ones pass**

Run: `uv run pytest tests/test_jev_router.py -q`
Expected: the 0.2.0 tests pass through Codex; the new Claude tests fail (Claude still takes the 0.2.0 path, `mode` is not a command).

- [ ] **Step 3: Implement in `jev_router.py`**

Imports (top, after the stdlib imports): `import functools`, `import hashlib`, and

```python
# Reason: sibling modules, found because `uv run --script` puts this folder first on sys.path.
import delegation
import session as sessions
import usage
```

Constants and data (after `JOBS, TIERS = load_tiers()`):

```python
MODES = ("shadow", "capture", "live")
STEPS_INSTRUCTIONS = (
    "An AI coding agent receives this message. How many steps will it take to finish what the message asks? "
    "One step is one model call: reading a file, running a command, making an edit, delegating, or writing the reply."
)
STEP_BUCKETS = [
    "1 step: a direct answer from what is already known, no tools",
    "2 to 3 steps: one quick lookup, command or small edit",
    "4 to 10 steps: read or edit a few files, run a test",
    "11 to 30 steps: a feature, a debugging session, or a change across several files",
    "more than 30 steps: a long autonomous build, migration, review loop or investigation",
]


@functools.cache
def calibration() -> tuple[delegation.Bin, ...]:
    """Loaded on first use inside the guarded Claude route, so a broken file can only
    cost that message its opinion, never block it or touch Codex and opencode."""
    override = os.environ.get("JEV_ROUTER_CALIBRATION")
    return delegation.load_calibration(Path(override) if override else None)


@functools.cache
def prices() -> dict[str, usage.Prices]:
    return usage.load_prices()
```

Replace `PRIVACY` with:

```python
PRIVACY = (
    "While it is on, the text of every message you send also goes to OpenRouter and to TypeSafe "
    "(the company that makes Jev), and in Claude Code so does the agent's previous reply (its last "
    "1,500 characters). Keep it off for private work."
)
```

Give `ask_jev` two optional parameters and use them in the body:

```python
def ask_jev(
    message: str, tiers: tuple[Tier, ...], key: str, timeout: float, *, state: dict | None = None,
    questions: dict | None = None,
) -> dict:
```

with `"state": state or {"message": message[:MAX_MESSAGE_CHARS]}` and `"questions": questions or jev_questions(tiers)`.

Add after `parse_verdict`:

```python
def steps_questions(tiers: tuple[Tier, ...]) -> dict:
    """The cache-aware questions: how many calls the job takes, and which tier fits it."""
    return {
        "steps": {"type": "score", "instructions": STEPS_INSTRUCTIONS, "criteria": STEP_BUCKETS},
        "size": jev_questions(tiers)["size"],
    }


@dataclass(frozen=True)
class StepsVerdict:
    steps: float  # Jev's step score: 0 is one call, 4 is more than 30
    size: str
    probability: float


def parse_steps_verdict(body: object, tiers: tuple[Tier, ...]) -> StepsVerdict:
    try:
        steps_answer = body["answers"]["steps"]  # type: ignore[index]
        size_answer = body["answers"]["size"]  # type: ignore[index]
        score = steps_answer["score"]
        size = size_answer["choice"]
        picked = size_answer["probabilities"][size]
    except (KeyError, IndexError, TypeError):
        raise BadAnswer("no steps or size answer") from None
    if steps_answer.get("type") != "score" or size_answer.get("type") != "choice":
        raise BadAnswer("an answer has the wrong type")
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= len(STEP_BUCKETS) - 1:
        raise BadAnswer("the step score is not a number from 0 to 4")
    if size not in {t.size for t in tiers}:
        raise BadAnswer("unknown size")
    return StepsVerdict(steps=float(score), size=size, probability=probability(picked))
```

Add after `keep_note`:

```python
def delegate_note(tier: Tier, decision: delegation.Decision, context: int) -> str:
    share = decision.expected_saving / decision.expected_keep
    return (
        f"Jev router: this job will likely take about {decision.median_calls} model calls, and every call here "
        f"re-reads this conversation's {context // 1000}k tokens. Doing it in a fresh subagent is expected to save "
        f"${decision.expected_saving:.2f} ({share:.0%} of doing it here), with a {decision.loss_probability:.0%} "
        "chance it costs more. Write a self-contained brief (the goal, the files and decisions that matter, and "
        f"what done looks like) and give it to the `{tier.helper}` subagent, never the `fork` type, which copies "
        "this whole conversation. Tell it not to commit, push, open pull requests or deploy, and to list what is "
        "left. Then review its changes and do those steps yourself. Keep the job here only if it needs the user "
        "in the loop or cannot be briefed."
    )


def route_cache_aware(config: dict, payload: dict, prompt: str) -> str:
    """Claude Code: price keeping the job against a fresh subagent. Only `live` mode
    tells the session anything; `shadow` and `capture` only log."""
    mode = config.get("mode", "shadow")
    event: dict = {
        "harness": "claude",
        "version": 3,
        "mode": mode,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        # Reason: lets the shadow report find the message in the transcript
        # without the log ever holding its text.
        "prompt_sha": hashlib.sha256(prompt.encode()).hexdigest()[:16],
    }
    transcript = payload.get("transcript_path")
    current = sessions.read_session(Path(transcript)) if transcript else None
    if current is None:
        event["outcome"] = "no_session"
        record(event)
        return ""
    event.update(context=current.context, model=current.model, added=current.added, output=current.output)
    tiers = TIERS["claude"]
    started = time.monotonic()
    body = verdict = None
    try:
        key = read_key(config.get("key_file"))
        if not key:
            raise NoKey("no OpenRouter API key in the key file")
        state = {"agent_previous_reply": current.previous_reply, "message": prompt[:MAX_MESSAGE_CHARS]}
        body = ask_jev(prompt, tiers, key, timeout_of(config), state=state, questions=steps_questions(tiers))
        verdict = parse_steps_verdict(body, tiers)
    except TimeoutError:
        event["outcome"] = "timeout"
    except Exception as exc:  # any failure means "no opinion"
        event.update(outcome="error", error=error_label(exc))
    event["latency_ms"] = round((time.monotonic() - started) * 1000)
    if body is not None:
        event.update(answered=True, cost=cost_of(body))
    note = None
    if verdict is not None:
        tier = next(t for t in tiers if t.size == verdict.size)
        parent, helper = prices().get(current.model), prices().get(tier.model_id)
        event.update(steps=verdict.steps, size=verdict.size, helper=tier.helper, helper_model=tier.model_id)
        if parent is None or helper is None:
            event["outcome"] = "unpriced"
        else:
            sample = delegation.calls_for(calibration(), verdict.steps)
            decision = delegation.decide(
                sample,
                current.context,
                current.added,
                current.output,
                parent,
                helper,
                same_model=tier.model_id == current.model,
            )
            event.update(
                outcome="delegate" if decision.delegate else "keep",
                expected_keep=round(decision.expected_keep, 4),
                expected_saving=round(decision.expected_saving, 4),
                loss_probability=round(decision.loss_probability, 4),
                median_calls=decision.median_calls,
                calls=list(sample),
            )
            if decision.delegate:
                note = delegate_note(tier, decision, current.context)
    record(event)
    return hook_json(note) if note and mode == "live" else ""
```

In `route()`, right after the slash-command check, add:

```python
    if harness == "claude":
        return route_cache_aware(config, payload, prompt)
```

Add the command:

```python
def cmd_mode(mode: str, directory: str | None) -> int:
    config = load_config()
    config["mode"] = mode
    if directory:
        config["fork_check_dir"] = str(Path(directory).expanduser().resolve())
    save_config(config)
    where = config.get("fork_check_dir") or str(home() / "fork-check")
    print(
        {
            "shadow": "Jev router mode: shadow. In Claude Code it decides and logs, and tells the session nothing.",
            "capture": f"Jev router mode: capture. Like shadow, and it saves a snapshot of each job it would "
            f"delegate, under {where}.",
            "live": "Jev router mode: live. In Claude Code it tells the session to hand long jobs to a fresh subagent.",
        }[mode]
    )
    return 0
```

and wire it in `main()`: `mode = sub.add_parser("mode", help="shadow, capture or live (Claude Code)")`, `mode.add_argument("mode", choices=MODES)`, `mode.add_argument("--dir", help="where capture saves snapshots")`, and `if args.command == "mode": return cmd_mode(args.mode, args.dir)`.

In `status_text`, before the cost paragraph, add:

```python
    cache_aware = [e for e in events if e.get("version") == 3]
    if cache_aware:
        delegated = sum(e.get("outcome") == "delegate" for e in cache_aware)
        lines.append(
            f"\nClaude Code, mode {config.get('mode', 'shadow')}: {len(cache_aware)} messages decided, "
            f"{delegated} worth a fresh subagent, {sum('snapshot' in e for e in cache_aware)} snapshots."
        )
```

- [ ] **Step 4: Tell the helpers the new boundary**

In each of `jev-router/agents/{tiny,everyday,large,hardest}.md`, replace the paragraph starting "Do the whole job with the tools you have" with:

```markdown
Do the whole job with the tools you have, then report the result plainly, the way the main session should pass it on to the user. Do not commit, push, open pull requests or deploy: stop before any such step and list it at the end of your report, so the main session can review your changes and do it. If the job needs context you were not given, say exactly what is missing instead of guessing.
```

- [ ] **Step 5: Run everything**

Run: `uv run pytest tests/ -q && uv run ruff check scripts/ tests/ jev-router/ && uv run ruff format --check . && uv run pyright`
Expected: all pass (the opencode tests too: opencode still takes the 0.2.0 path).

- [ ] **Step 6: Commit**

```bash
git add jev-router/ tests/test_jev_router.py
git commit -m "jev-router: cache-aware decisions for Claude Code in shadow and live mode"
```

---

### Task 7: Snapshots in capture mode (`snapshot.py`)

**Files:**
- Create: `jev-router/skills/jev/scripts/snapshot.py`
- Modify: `jev-router/skills/jev/scripts/jev_router.py` (`route_cache_aware`)
- Test: `tests/test_jev_snapshot.py`, `tests/test_jev_router.py`

**Interfaces:**
- Consumes: `usage.is_typed`, `usage.entry_text`; the version-3 event fields from Task 6.
- Produces: `SnapshotError`; `take_snapshot(root: Path, payload: dict, prompt: str, note: str, event: dict) -> str` (the snapshot id); `trim_before_prompt(data: bytes, prompt: str) -> bytes`; `ignored_fingerprint(top: Path) -> str`; `git(cwd: Path, *args: str) -> str`. Snapshot layout under `<root>/snapshots/<id>/`: `meta.json`, `transcript.jsonl`, `message.txt`, `note.txt`, `changes.diff`, `untracked.tar`. `meta.json` keys: `id, created, session_id, transcript_path, cwd, toplevel, head, branch, ignored_fingerprint, context, model, helper, expected_saving, loss_probability, median_calls`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_snapshot.py`)

```python
"""Fork-check snapshots: the conversation and working copy just before a job."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import snapshot  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402

EVENT = {"context": 803_010, "model": "claude-opus-5-5", "helper": "jev-router:large", "expected_saving": 1.2,
         "loss_probability": 0.0, "median_calls": 30}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "work" / "app"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "T")
    (r / ".gitignore").write_text("node_modules/\n.venv/\n.env\n__pycache__/\n")
    (r / "app.py").write_text("print('v1')\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    (r / "app.py").write_text("print('v2')\n")  # uncommitted change
    (r / "notes.md").write_text("draft\n")  # untracked
    (r / "node_modules").mkdir()
    (r / "node_modules" / ".package-lock.json").write_text("{}")
    (r / ".env").write_text("TOKEN=x\n")
    return r


def payload(repo: Path, transcript: Path) -> dict:
    return {"session_id": "abcdef1234", "transcript_path": str(transcript), "cwd": str(repo)}


def write(path: Path, entries: list[dict]) -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path


def test_snapshot_holds_the_conversation_and_the_working_copy(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text="Ready.")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "NOTE", EVENT)
    d = tmp_path / "fc" / "snapshots" / sid
    meta = json.loads((d / "meta.json").read_text())
    assert meta["head"] == git(repo, "rev-parse", "HEAD").strip()
    assert meta["branch"] == "main" and meta["toplevel"] == str(repo.resolve())
    assert meta["helper"] == "jev-router:large" and meta["context"] == 803_010
    assert (d / "message.txt").read_text() == "build it"
    assert (d / "note.txt").read_text() == "NOTE"
    assert "print('v2')" in (d / "changes.diff").read_text()
    with tarfile.open(d / "untracked.tar") as tar:
        assert tar.getnames() == ["notes.md"]
    assert (d / "transcript.jsonl").read_text() == transcript.read_text()


def test_snapshot_cuts_the_prompt_if_already_written(tmp_path: Path, repo: Path) -> None:
    before = [typed("start"), assistant("r1", text="Ready.")]
    transcript = write(tmp_path / "t.jsonl", [*before, typed("build it")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    kept = (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text()
    assert kept == "".join(json.dumps(e) + "\n" for e in before)


def test_snapshot_keeps_everything_if_prompt_not_written_yet(tmp_path: Path, repo: Path) -> None:
    entries = [typed("build it"), assistant("r1", text="Done."), typed("something else")]
    transcript = write(tmp_path / "t.jsonl", entries)
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    assert (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text() == transcript.read_text()


def test_snapshot_keeps_an_earlier_identical_prompt(tmp_path: Path, repo: Path) -> None:
    transcript = write(tmp_path / "t.jsonl", [typed("continue"), assistant("r1", text="Done.")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "continue", "N", EVENT)
    assert (tmp_path / "fc" / "snapshots" / sid / "transcript.jsonl").read_text() == transcript.read_text()


def test_fingerprint_sees_python_packages_come_and_go(repo: Path) -> None:
    packages = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    packages.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
    before = snapshot.ignored_fingerprint(repo)
    (packages / "requests").mkdir()
    assert snapshot.ignored_fingerprint(repo) != before


def test_fingerprint_follows_dependencies_and_env_files_only(repo: Path) -> None:
    before = snapshot.ignored_fingerprint(repo)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"0")
    assert snapshot.ignored_fingerprint(repo) == before
    (repo / ".env").write_text("TOKEN=y\n")
    assert snapshot.ignored_fingerprint(repo) != before


def test_outside_a_git_repository_there_is_no_snapshot(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.take_snapshot(tmp_path / "fc", payload(plain, transcript), "x", "N", EVENT)


def test_huge_untracked_files_are_refused(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "MAX_UNTRACKED_BYTES", 3)
    transcript = write(tmp_path / "t.jsonl", [assistant("r1")])
    with pytest.raises(snapshot.SnapshotError, match="untracked"):
        snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "x", "N", EVENT)
```

And in `tests/test_jev_router.py`:

```python
def test_capture_mode_snapshots_a_job_it_would_delegate(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=T", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    switch_on(home, jev, mode="capture", fork_check_dir=str(tmp_path / "fc"))
    jev.raw_answers = steps_answers(3.5)
    transcript = home.parent / "claude-transcript.jsonl"
    transcript.write_text(json.dumps(assistant("r1", read=800_000, text="Ready.")) + "\n")
    calibration = home.parent / "calibration.json"
    calibration.write_text(json.dumps(CALIBRATION))
    payload = {"prompt": "build it", "session_id": "sess-1", "transcript_path": str(transcript), "cwd": str(repo)}
    result = run(home, jev, "hook", "claude", stdin=json.dumps(payload),
                 env={"JEV_ROUTER_CALIBRATION": str(calibration), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    assert result.stdout == ""
    (event,) = log(home)
    assert (tmp_path / "fc" / "snapshots" / event["snapshot"] / "meta.json").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_snapshot.py tests/test_jev_router.py -q -k "snapshot or fingerprint or capture or untracked or outside"`
Expected: `ModuleNotFoundError: No module named 'snapshot'` and the capture test failing.

- [ ] **Step 3: Implement `snapshot.py`**

```python
"""Capture what a fork-check replay needs, from inside the hook, before the turn runs.

The turn starts editing files seconds after the hook returns, so everything here is
fast: a transcript copy, `git diff`, a tar of untracked files, and a cheap
fingerprint of the ignored setup files and dependencies (sizes and modification
times, never their contents except small `.env` files).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

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


def git(cwd: Path, *args: str) -> str:
    return _git_bytes(cwd, *args).decode()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=10)
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


def ignored_fingerprint(top: Path) -> str:
    """Changes when installed dependencies or `.env` files change, not when caches do."""
    listing = _git_bytes(top, "status", "--ignored", "--porcelain=v1", "-z", "--untracked-files=normal")
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


def take_snapshot(root: Path, payload: dict, prompt: str, note: str, event: dict) -> str:
    cwd = Path(payload["cwd"])
    top = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
    head = git(top, "rev-parse", "HEAD").strip()
    branch = git(top, "rev-parse", "--abbrev-ref", "HEAD").strip()
    untracked = [p for p in _git_bytes(top, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    if sum((top / p.decode()).stat().st_size for p in untracked) > MAX_UNTRACKED_BYTES:
        raise SnapshotError("untracked files are too large to snapshot")
    now = datetime.now(UTC)
    sid = f"{now:%Y%m%d-%H%M%S}-{str(payload['session_id'])[:8]}"
    d = root / "snapshots" / sid
    d.mkdir(parents=True)
    transcript = Path(payload["transcript_path"])
    (d / "transcript.jsonl").write_bytes(trim_before_prompt(transcript.read_bytes(), prompt))
    (d / "message.txt").write_text(prompt)
    (d / "note.txt").write_text(note)
    (d / "changes.diff").write_bytes(_git_bytes(top, "diff", "--binary", "HEAD"))
    with tarfile.open(d / "untracked.tar", "w") as tar:
        for p in untracked:
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
        "ignored_fingerprint": ignored_fingerprint(top),
        **{k: event.get(k) for k in ("context", "model", "helper", "expected_saving", "loss_probability", "median_calls")},
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return sid
```

- [ ] **Step 4: Call it from the hook**

In `jev_router.py` add `import snapshot` next to the other sibling imports, and in `route_cache_aware`, just before `record(event)`:

```python
    if note and mode == "capture":
        root = Path(config.get("fork_check_dir") or home() / "fork-check")
        try:
            event["snapshot"] = snapshot.take_snapshot(root, payload, prompt, note, event)
        except Exception as exc:  # a failed snapshot never touches the message
            event["snapshot_error"] = error_label(exc) if not isinstance(exc, snapshot.SnapshotError) else str(exc)
```

- [ ] **Step 5: Run**

Run: `uv run pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add jev-router/skills/jev/scripts/snapshot.py jev-router/skills/jev/scripts/jev_router.py tests/test_jev_snapshot.py tests/test_jev_router.py
git commit -m "jev-router: capture mode snapshots the jobs it would delegate"
```

---

### Task 8: Shadow report and the runner's skeleton

**Files:**
- Create: `jev-router/skills/jev/scripts/report.py`, `jev-router/skills/jev/scripts/fork_check.py`
- Test: `tests/test_jev_report.py`

**Interfaces:**
- Consumes: version-3 log events (Task 6), `usage`, `delegation`.
- Produces: `report.prompt_sha(text: str) -> str`; `report.turn_after(entries: list[dict], sha: str, near: str | None = None) -> list[dict] | None` (with `near`, the occurrence closest in time); `report.period_events(events: list[dict], points: list[dict]) -> list[dict]`; `report.shadow_rows(events: list[dict], prices: dict) -> list[dict]` (keys `ts, outcome, steps, median_calls, real_calls, real_cost, would_lose`); `report.shadow_report(events: list[dict], prices: dict) -> str`; `fork_check.root() -> Path`; `fork_check.main(argv: list[str] | None = None) -> int` with the `shadow` subcommand.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_report.py`)

```python
"""Shadow mode's decisions against what the turns really did."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import report  # noqa: E402
import usage  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def event(transcript: Path, prompt: str, outcome: str = "delegate") -> dict:
    return {"ts": "2026-10-01T10:00:00+00:00", "harness": "claude", "version": 3, "outcome": outcome,
            "transcript_path": str(transcript), "prompt_sha": report.prompt_sha(prompt), "context": 400_000,
            "model": "claude-opus-5-5", "helper_model": "claude-opus-5-5", "added": 3_000, "output": 1_000,
            "steps": 3.2, "median_calls": 30, "answered": True, "cost": 0.00002, "latency_ms": 300}


def test_turn_runs_from_the_message_to_the_next_typed_one() -> None:
    entries = [typed("a"), assistant("r1"), typed("b"), assistant("r2"), assistant("r3"), typed("c")]
    turn = report.turn_after(entries, report.prompt_sha("b"))
    assert turn is not None and len(usage.calls(turn)) == 2


def test_a_repeated_prompt_finds_the_turn_nearest_in_time() -> None:
    first = {**typed("continue"), "timestamp": "2026-10-01T09:00:00Z"}
    second = {**typed("continue"), "timestamp": "2026-10-01T10:00:00Z"}
    entries = [first, assistant("r1"), second, assistant("r2"), assistant("r3")]
    turn = report.turn_after(entries, report.prompt_sha("continue"), near="2026-10-01T10:00:01+00:00")
    assert turn is not None and len(usage.calls(turn)) == 2


def test_rows_join_decisions_with_real_calls_and_cost(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    entries = [typed("build"), *[assistant(f"r{i}", read=400_000) for i in range(25)], typed("next")]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    (row,) = report.shadow_rows([event(transcript, "build")], usage.load_prices())
    assert row["real_calls"] == 25
    assert row["real_cost"] > 0
    assert row["would_lose"] is False


def test_report_counts_decisions_and_jev_overhead(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(typed("x")) + "\n")
    events = [event(transcript, "x"), event(transcript, "y", outcome="keep"), {"version": 3, "outcome": "no_session"}]
    text = report.shadow_report(events, usage.load_prices())
    assert "Messages seen: 3; decided: 2; worth a fresh subagent: 1 (50%)." in text
    assert "Jev: 2 answered calls, $0.0000, 0.6 s of added wait in total." in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_report.py -q`
Expected: `ModuleNotFoundError: No module named 'report'`.

- [ ] **Step 3: Implement `report.py`** (the fork-check half is added in Task 13)

```python
"""Reports: shadow mode's decisions against what really happened, and the fork check's verdict."""

from __future__ import annotations

import hashlib
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import delegation
import usage


def prompt_sha(text: str) -> str:
    """The hook logs this, never the text."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def _when(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def turn_after(entries: list[dict], sha: str, near: str | None = None) -> list[dict] | None:
    """From the typed message with this hash to the next typed message. With `near`,
    the occurrence closest in time to it, so a repeated "continue" finds its own turn."""
    typed_at = [i for i, e in enumerate(entries) if usage.is_typed(e)]
    matches = [i for i in typed_at if prompt_sha(usage.entry_text(entries[i]) or "") == sha]
    if not matches:
        return None
    start = matches[0]
    target = _when(near) if near else None
    if target is not None:

        def distance(i: int) -> float:
            when = _when(str(entries[i].get("timestamp", "")))
            return abs((when - target).total_seconds()) if when else float("inf")

        start = min(matches, key=distance)
    end = next((i for i in typed_at if i > start), len(entries))
    return entries[start:end]


def period_events(events: list[dict], points: list[dict]) -> list[dict]:
    """Every cache-aware hook event from the first scored job's capture to the last
    one's, plus a minute: the last hook logs its event just after its snapshot."""
    if not points:
        return []
    first = datetime.fromisoformat(points[0]["meta"]["created"])
    last = datetime.fromisoformat(points[-1]["meta"]["created"]) + timedelta(seconds=60)
    return [e for e in events if e.get("version") == 3 and (t := _when(str(e.get("ts", "")))) and first <= t <= last]


def shadow_rows(events: list[dict], prices: dict) -> list[dict]:
    rows = []
    cache: dict[str, list[dict]] = {}
    for e in events:
        if e.get("version") != 3 or e.get("outcome") not in ("keep", "delegate"):
            continue
        path = e["transcript_path"]
        if path not in cache:
            cache[path] = usage.read_entries(Path(path)) if Path(path).exists() else []
        turn = turn_after(cache[path], e["prompt_sha"], near=e.get("ts"))
        if turn is None:
            continue
        calls = usage.calls(turn)
        k = len(calls)
        parent, helper = prices.get(e["model"]), prices.get(e["helper_model"])
        would_lose = None
        if parent and helper and k:
            scale = 1.0 if e["model"] == e["helper_model"] else delegation.OTHER_MODEL_SCALE
            keep = delegation.keep_cost(k, e["context"], e["added"], e["output"], parent)
            would_lose = delegation.delegate_cost(k, e["context"], e["added"], e["output"], parent, helper, scale) > keep
        rows.append(
            {
                "ts": e["ts"],
                "outcome": e["outcome"],
                "steps": e["steps"],
                "median_calls": e["median_calls"],
                "real_calls": k,
                "real_cost": sum(c.cost(prices) or 0.0 for c in calls),
                "would_lose": would_lose,
            }
        )
    return rows


def shadow_report(events: list[dict], prices: dict) -> str:
    seen = [e for e in events if e.get("version") == 3]
    decided = [e for e in seen if e.get("outcome") in ("keep", "delegate")]
    chosen = sum(e["outcome"] == "delegate" for e in decided)
    answered = [e for e in seen if e.get("answered")]
    share = f"({chosen / len(decided):.0%})" if decided else "(none yet)"
    lines = [
        "# Shadow report",
        "",
        f"Messages seen: {len(seen)}; decided: {len(decided)}; worth a fresh subagent: {chosen} {share}.",
        f"Jev: {len(answered)} answered calls, ${sum(e.get('cost') or 0 for e in answered):.4f}, "
        f"{sum(e.get('latency_ms') or 0 for e in seen) / 1000:.1f} s of added wait in total.",
    ]
    selected = [r for r in shadow_rows(seen, prices) if r["outcome"] == "delegate"]
    if selected:
        lost = sum(bool(r["would_lose"]) for r in selected)
        lines.append(
            f"Selected jobs found in their transcript: {len(selected)}. Median predicted "
            f"{statistics.median(r['median_calls'] for r in selected):.0f} calls, median real "
            f"{statistics.median(r['real_calls'] for r in selected):.0f}. At their real length the cost model says "
            f"{lost} ({lost / len(selected):.0%}) would have cost more delegated."
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Create `fork_check.py` with the `shadow` command**

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""The fork check: replay the jobs the router would delegate, both ways, and report.

  fork_check.py shadow                     shadow-mode decisions against what really happened
  fork_check.py list                       snapshots, their status, and what each real turn did outside
  fork_check.py check                      restore-check new snapshots without running anything
  fork_check.py mark ID (safe|skip) [--reason TEXT]
  fork_check.py replay (ID | --next) [--trial]
  fork_check.py judge ID                   blind Codex judge (before publishing)
  fork_check.py publish ID                 push both results to a private copy and open the comparison PR
  fork_check.py report                     the fork check's pass or fail over 20 jobs
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import jev_router
import report
import usage


def root() -> Path:
    config = jev_router.load_config()
    return Path(config.get("fork_check_dir") or jev_router.home() / "fork-check")


def set_status(sid: str, status: str, reason: str = "") -> None:
    path = root() / "status.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "id": sid, "status": status, "reason": reason}
    with path.open("a") as handle:
        handle.write(json.dumps(line) + "\n")


def statuses() -> dict[str, dict]:
    path = root() / "status.jsonl"
    latest: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            latest[entry["id"]] = entry
    return latest


def snapshot_dirs() -> list[Path]:
    return sorted(p for p in (root() / "snapshots").glob("*") if (p / "meta.json").exists())


def cmd_shadow() -> int:
    print(report.shadow_report(jev_router.read_log(), usage.load_prices()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shadow")
    args = parser.parse_args(argv)
    if args.command == "shadow":
        return cmd_shadow()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_jev_report.py -q && uv run pyright`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add jev-router/skills/jev/scripts/report.py jev-router/skills/jev/scripts/fork_check.py tests/test_jev_report.py
git commit -m "jev-router: shadow report and the fork-check runner skeleton"
```

---

### Task 9: Restore a snapshot, and the same-day restore check

**Files:**
- Create: `jev-router/skills/jev/scripts/replay.py`
- Modify: `jev-router/skills/jev/scripts/fork_check.py` (`check`)
- Test: `tests/test_jev_replay.py`

**Interfaces:**
- Consumes: `snapshot.ignored_fingerprint`, `snapshot.git`, the snapshot layout (Task 7).
- Produces: `replay.Inconclusive`; `replay.restore(snap: Path, dest: Path, *, copy_ignored: bool = True) -> Path` (the clone; `dest/origin.git` is its bare origin; the start state is the commit at `refs/jev/start`); `replay.ignored_entries(top: Path) -> list[str]`; `fork_check.cmd_check() -> int`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_replay.py`; reuse the `repo` fixture and helpers by importing them)

```python
"""Restoring a snapshot into a clone, and replaying it both ways."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import replay  # noqa: E402
import snapshot  # noqa: E402
from test_jev_snapshot import EVENT, git, payload, repo, write  # noqa: E402, F401
from test_jev_usage import assistant, typed  # noqa: E402


@pytest.fixture
def snap(tmp_path: Path, repo: Path) -> Path:
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1", text=f"Edited {repo}/app.py")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "NOTE `jev-router:large`", EVENT)
    return tmp_path / "fc" / "snapshots" / sid


def test_restore_rebuilds_the_working_copy_with_a_local_origin(tmp_path: Path, repo: Path, snap: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    (repo / "later.py").write_text("x\n")
    git(repo, "add", "later.py")
    git(repo, "commit", "-qm", "later work")  # the user moved on after the snapshot
    clone = replay.restore(snap, tmp_path / "r")
    assert git(clone, "rev-parse", "HEAD").strip() == head
    assert (clone / "app.py").read_text() == "print('v2')\n"
    assert (clone / "notes.md").read_text() == "draft\n"
    assert not (clone / "later.py").exists()
    assert (clone / ".env").read_text() == "TOKEN=x\n"
    assert (clone / "node_modules" / ".package-lock.json").exists()
    assert git(clone, "remote", "get-url", "origin").strip() == str(tmp_path / "r" / "origin.git")
    assert git(clone, "status", "--porcelain").splitlines() == [" M app.py", "?? notes.md"]
    assert git(clone, "show", "refs/jev/start:app.py") == "print('v2')\n"
    assert git(clone, "show", "refs/jev/start:notes.md") == "draft\n"
    assert git(clone, "rev-parse", "refs/jev/start^").strip() == head


def test_changed_dependencies_make_the_job_inconclusive(tmp_path: Path, repo: Path, snap: Path) -> None:
    (repo / ".env").write_text("TOKEN=changed\n")
    with pytest.raises(replay.Inconclusive, match="changed since the snapshot"):
        replay.restore(snap, tmp_path / "r")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_replay.py -q`
Expected: `ModuleNotFoundError: No module named 'replay'`.

- [ ] **Step 3: Implement `replay.py` (restore half)**

```python
"""Replay a snapshot both ways: restore it into clones, run keep and delegate, measure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import snapshot

IDENTITY = ["-c", "user.name=jev fork check", "-c", "user.email=jev-fork-check@localhost"]


class Inconclusive(RuntimeError):
    """The job cannot be replayed faithfully; it is replaced by the next marked one."""


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    result = subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def ignored_entries(top: Path) -> list[str]:
    listing = run("git", "-C", str(top), "status", "--ignored", "--porcelain=v1", "-z", "--untracked-files=normal")
    return sorted(e[3:].rstrip("/") for e in listing.split("\0") if e.startswith("!! "))


def copy_tree(source: Path, target: Path) -> None:
    """A copy-on-write clone on macOS, so node_modules costs no time or space."""
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = ["-cRp"] if sys.platform == "darwin" else ["-a", "--reflink=auto"]
    run("cp", *flags, str(source), str(target))


def restore(snap: Path, dest: Path, *, copy_ignored: bool = True) -> Path:
    meta = json.loads((snap / "meta.json").read_text())
    top, head = Path(meta["toplevel"]), meta["head"]
    if copy_ignored and snapshot.ignored_fingerprint(top) != meta["ignored_fingerprint"]:
        raise Inconclusive("dependencies or .env files changed since the snapshot")
    dest.mkdir(parents=True)
    bare, clone = dest / "origin.git", dest / "repo"
    run("git", "clone", "-q", "--bare", str(top), str(bare))
    branch = meta["branch"] if meta["branch"] != "HEAD" else "jev-snapshot"
    if subprocess.run(["git", "-C", str(bare), "cat-file", "-e", head], capture_output=True).returncode != 0:
        run("git", "-C", str(bare), "fetch", "-q", str(top), head)
    # Reason: the branch may have moved on since the snapshot; point it back so the
    # clone checks out exactly the snapshot's commit.
    run("git", "-C", str(bare), "update-ref", f"refs/heads/{branch}", head)
    run("git", "clone", "-q", "--branch", branch, str(bare), str(clone))
    diff = snap / "changes.diff"
    if diff.stat().st_size:
        run("git", "-C", str(clone), "apply", "--binary", str(diff))
    with tarfile.open(snap / "untracked.tar") as tar:
        tar.extractall(clone, filter="data")
    # The start state as a commit, without touching the index or the working tree,
    # so the judge can diff each result against exactly where the agent began.
    env = {**os.environ, "GIT_INDEX_FILE": str(dest / "start.index")}
    run("git", "-C", str(clone), "add", "-A", env=env)
    tree = run("git", "-C", str(clone), "write-tree", env=env).strip()
    start = run("git", "-C", str(clone), *IDENTITY, "commit-tree", tree, "-p", head, "-m", "jev fork check: start").strip()
    run("git", "-C", str(clone), "update-ref", "refs/jev/start", start)
    if copy_ignored:
        for rel in ignored_entries(top):
            copy_tree(top / rel, clone / rel)
    return clone
```

(`git commit-tree` takes identity from `-c` options before the subcommand; `IDENTITY` is placed there.)

- [ ] **Step 4: Add `check` to `fork_check.py`**

```python
def cmd_check() -> int:
    """Restore every snapshot not yet checked, without running anything, and compare
    it: the commit, the uncommitted changes and the untracked files."""
    import shutil
    import tarfile

    import replay

    done = statuses()
    for snap in snapshot_dirs():
        sid = snap.name
        if sid in done:
            continue
        dest = root() / "checks" / sid
        try:
            clone = replay.restore(snap, dest, copy_ignored=False)
            meta = json.loads((snap / "meta.json").read_text())
            with tarfile.open(snap / "untracked.tar") as tar:
                captured = sorted(tar.getnames())
            listed = replay.run("git", "-C", str(clone), "ls-files", "--others", "--exclude-standard", "-z")
            ok = (
                replay.run("git", "-C", str(clone), "rev-parse", "HEAD").strip() == meta["head"]
                and replay.run("git", "-C", str(clone), "diff", "--binary", "HEAD") == (snap / "changes.diff").read_text()
                and sorted(p for p in listed.split("\0") if p) == captured
            )
            set_status(sid, "restore_ok" if ok else "restore_failed", "" if ok else "restored state differs")
        except Exception as exc:  # report and move on to the next snapshot
            set_status(sid, "restore_failed", str(exc)[:200])
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        print(f"{sid}: {statuses()[sid]['status']}")
    return 0
```

and register `sub.add_parser("check")` with `if args.command == "check": return cmd_check()`. Add a test:

```python
def test_check_marks_a_good_snapshot_restore_ok(tmp_path: Path, snap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fork_check

    monkeypatch.setattr(fork_check, "root", lambda: tmp_path / "fc")
    assert fork_check.main(["check"]) == 0
    assert fork_check.statuses()[snap.name]["status"] == "restore_ok"
    assert not (tmp_path / "fc" / "checks" / snap.name).exists()
```

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_jev_replay.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add jev-router/skills/jev/scripts/replay.py jev-router/skills/jev/scripts/fork_check.py tests/test_jev_replay.py
git commit -m "jev-router: restore snapshots into clones with a local origin"
```

---

### Task 10: List snapshots with their external actions, and mark them

**Files:**
- Modify: `jev-router/skills/jev/scripts/fork_check.py` (`list`, `mark`)
- Test: `tests/test_jev_fork_check.py`

**Interfaces:**
- Consumes: `report.turn_after`, `report.prompt_sha`, `usage.read_entries`.
- Produces: `fork_check.external_actions(turn: list[dict]) -> list[str]`; `fork_check.real_turn(meta: dict, message: str) -> list[dict]` (the real turn, subagent entries in its time range included); commands `list` and `mark ID (safe|skip) [--reason TEXT]`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_fork_check.py`)

```python
"""What a real turn did outside the machine, and the user's marks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402


def tool(name: str, **inputs: object) -> dict:
    return {"type": "assistant", "requestId": name, "message": {"content": [
        {"type": "tool_use", "name": name, "input": inputs}]}}


def test_external_actions_name_pushes_prs_deploys_and_mcp_writes() -> None:
    turn = [
        tool("Bash", command="git push origin HEAD"),
        tool("Bash", command="gh pr create --title x --body y"),
        tool("Bash", command="uv run pytest"),
        tool("mcp__econoplus-prod__execute_sql", query="update x"),
        tool("mcp__claude_ai_Gmail__get_message", id="1"),
        tool("Read", file_path="/x"),
    ]
    found = fork_check.external_actions(turn)
    assert found == ["shell: git push origin HEAD", "shell: gh pr create --title x --body y",
                     "MCP: mcp__econoplus-prod__execute_sql"]


def test_marks_are_recorded_and_the_latest_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    assert fork_check.main(["mark", "20261001-100000-abc", "skip", "--reason", "writes to production"]) == 0
    assert fork_check.main(["mark", "20261001-100000-abc", "safe"]) == 0
    assert fork_check.statuses()["20261001-100000-abc"]["status"] == "safe"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_fork_check.py -q`
Expected: `AttributeError: module 'fork_check' has no attribute 'external_actions'`, and `mark` unknown.

- [ ] **Step 3: Implement in `fork_check.py`**

```python
import re

EXTERNAL_SHELL = re.compile(
    r"\bgit\s+push\b|\bgh\s+(pr|issue|release|repo|secret|workflow)\s+(create|merge|edit|close|comment|delete|run|set)"
    r"|\bgh\s+api\b.*-X\s*(POST|PUT|PATCH|DELETE)|\bvercel\b|\bsupabase\s+(db\s+push|functions\s+deploy)"
    r"|\bpulumi\s+up\b|\bterraform\s+apply\b|\bcurl\b.*-X\s*(POST|PUT|PATCH|DELETE)|\bhttp\s+(POST|PUT|PATCH|DELETE)\b"
    r"|\bnpm\s+publish\b|\bdeploy\b",
    re.IGNORECASE,
)
# Reason: an MCP tool is listed unless its name says it only reads; the user decides.
READ_ONLY_MCP = re.compile(r"__(get|list|search|read|query_logs|fetch|describe|view|find)[_a-z]*$", re.IGNORECASE)


def external_actions(turn: list[dict]) -> list[str]:
    found = []
    for entry in turn:
        if entry.get("type") != "assistant":
            continue
        for block in (entry.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, inputs = str(block.get("name")), block.get("input") or {}
            if name == "Bash" and EXTERNAL_SHELL.search(str(inputs.get("command", ""))):
                found.append(f"shell: {str(inputs['command'])[:120]}")
            elif name.startswith("mcp__") and not READ_ONLY_MCP.search(name):
                found.append(f"MCP: {name}")
    return found


def real_turn(meta: dict, message: str) -> list[dict]:
    """The real turn in the user's session, plus its subagents' entries in the same time range."""
    path = Path(meta["transcript_path"])
    entries = usage.read_entries(path) if path.exists() else []
    turn = report.turn_after(entries, report.prompt_sha(message), near=meta["created"])
    if not turn:
        return []
    stamps = [e["timestamp"] for e in turn if isinstance(e.get("timestamp"), str)]
    subagents = path.with_suffix("") / "subagents"
    if stamps and subagents.exists():
        for f in sorted(subagents.glob("*.jsonl")):
            turn += [e for e in usage.read_entries(f) if stamps[0] <= str(e.get("timestamp", "")) <= stamps[-1]]
    return turn


def cmd_list() -> int:
    done = statuses()
    print("| snapshot | repo | expected saving | status | outside the machine (from the real turn) |")
    print("|---|---|---|---|---|")
    for snap in snapshot_dirs():
        meta = json.loads((snap / "meta.json").read_text())
        actions = external_actions(real_turn(meta, (snap / "message.txt").read_text()))
        shown = "; ".join(actions[:3]) + (f"; and {len(actions) - 3} more" if len(actions) > 3 else "")
        status = done.get(snap.name, {}).get("status", "new")
        print(f"| {snap.name} | {Path(meta['toplevel']).name} | ${meta['expected_saving']:.2f} | {status} | "
              f"{shown or 'nothing found'} |")
    print("\nThe list is a guide: a replay can do something the real turn did not. Mark a job safe only if two "
          "replays in a row could run without an external effect you would mind.")
    return 0


def cmd_mark(sid: str, mark: str, reason: str) -> int:
    set_status(sid, mark, reason)
    print(f"{sid}: {mark}")
    return 0
```

Register them in `main()`:

```python
    sub.add_parser("list")
    mark = sub.add_parser("mark")
    mark.add_argument("id")
    mark.add_argument("mark", choices=("safe", "skip"))
    mark.add_argument("--reason", default="")
```

and dispatch `list` to `cmd_list()`, `mark` to `cmd_mark(args.id, args.mark, args.reason)`.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_jev_fork_check.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add jev-router/skills/jev/scripts/fork_check.py tests/test_jev_fork_check.py
git commit -m "jev-router: list snapshots with their external actions and mark them"
```

---

### Task 11: Replay both sides and measure them

**Files:**
- Modify: `jev-router/skills/jev/scripts/replay.py`, `jev-router/skills/jev/scripts/fork_check.py` (`replay`)
- Create: `tests/fake_claude.py`
- Test: `tests/test_jev_replay.py`

**Interfaces:**
- Consumes: `restore` (Task 9), `usage`, the snapshot's `note.txt`, `message.txt`, `meta.json` (`helper`).
- Produces: `replay.workdir(meta: dict, clone: Path) -> Path` (the clone's counterpart of the session's own directory); `replay.project_dir(claude_home: Path, cwd: Path) -> Path`; `replay.install_session(snap: Path, clone: Path, claude_home: Path) -> str`; `replay.run_claude(clone, session_id, prompt, env_extra, claude_home) -> tuple[dict, float, int]`; `replay.measure(claude_home, clone, session_id, prompt, helper) -> dict` (keys `calls, cost, unpriced_calls, first_cache_read, first_context, delegated`); `replay.replay_pair(snap, work, claude_home, rng) -> dict` (keys `id, order, sides{keep,delegate}{attempt, clone, session_id, wall_seconds, exit_code, warm, ...measure}, inconclusive`); constants `WARMUP`, `ATTEMPTS = 3`; env `JEV_FORK_CHECK_CLAUDE` (the `claude` binary, read at call time so tests can set it); `DEFAULT_CLAUDE_HOME`.

- [ ] **Step 1: Write the fake `claude`** (`tests/fake_claude.py`)

```python
#!/usr/bin/env python3
"""A stand-in for `claude --resume ID --fork-session -p PROMPT`: forks the session file, appends the
prompt and one model call, and prints the JSON result. With JEV_ROUTER_NOTE_FILE set it hands the
job to the named helper and writes a subagent transcript. FAKE_CLAUDE_COLD=1 makes a non-warm-up
call read nothing from cache. FAKE_CLAUDE_LOG appends each call's details to a file."""

import json
import os
import re
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
sid, prompt = args[args.index("--resume") + 1], args[args.index("-p") + 1]
home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
pdir = home / "projects" / re.sub(r"[^A-Za-z0-9-]", "-", os.getcwd())
entries = [json.loads(line) for line in (pdir / f"{sid}.jsonl").read_text().splitlines()]
new = str(uuid.uuid4())
warm = prompt.startswith("Reply with the single word")
cold = os.environ.get("FAKE_CLAUDE_COLD") == "1" and not warm
note = os.environ.get("JEV_ROUTER_NOTE_FILE")
entries.append({"type": "user", "sessionId": new, "origin": {"kind": "human"}, "message": {"content": prompt}})
content: list[dict] = [{"type": "text", "text": "ok"}]
if note and not warm:
    helper = re.search(r"`(jev-router:[a-z]+)`", Path(note).read_text())
    content = [{"type": "tool_use", "id": "t1", "name": "Agent",
                "input": {"subagent_type": helper.group(1) if helper else "?", "prompt": "brief"}}]
    sub = pdir / new / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps({"type": "assistant", "isSidechain": True, "requestId": "s1",
        "message": {"model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 5,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 25_000, "output_tokens": 500}}}) + "\n")
ctx = 100_000
entries.append({"type": "assistant", "sessionId": new, "requestId": f"r-{new}", "message": {
    "model": "claude-opus-5-5", "content": content, "usage": {"input_tokens": 5,
    "cache_read_input_tokens": 0 if cold else ctx, "cache_creation_input_tokens": ctx if cold else 200,
    "output_tokens": 50}}})
(pdir / f"{new}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
if not warm:
    Path("RESULT.txt").write_text(f"done by {'delegate' if note else 'keep'}\n")
if log := os.environ.get("FAKE_CLAUDE_LOG"):
    with open(log, "a") as handle:
        handle.write(json.dumps({"args": args, "cwd": os.getcwd(), "note": note, "router": os.environ.get("JEV_ROUTER")}) + "\n")
print(json.dumps({"type": "result", "session_id": new, "result": "ok"}))
```

- [ ] **Step 2: Write the failing tests** (append to `tests/test_jev_replay.py`)

```python
import random  # noqa: E402

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FAKE.chmod(0o755)
    monkeypatch.setenv("JEV_FORK_CHECK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.jsonl"))
    return tmp_path / "claude-home"


def test_install_session_rewrites_paths_and_ids(tmp_path: Path, repo: Path, snap: Path) -> None:
    clone = replay.restore(snap, tmp_path / "r")
    sid = replay.install_session(snap, clone, tmp_path / "claude-home")
    text = (replay.project_dir(tmp_path / "claude-home", clone) / f"{sid}.jsonl").read_text()
    assert str(repo.resolve()) not in text
    assert f"Edited {clone}/app.py" in text
    assert all(json.loads(line).get("sessionId", sid) == sid for line in text.splitlines())


def test_pair_runs_both_sides_and_only_delegate_gets_the_note(tmp_path: Path, snap: Path, fake_claude: Path) -> None:
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert not result["inconclusive"]
    keep, delegate = result["sides"]["keep"], result["sides"]["delegate"]
    assert keep["warm"] and delegate["warm"]
    assert delegate["delegated"] is True and keep["delegated"] is False
    assert delegate["calls"] == 2 and keep["calls"] == 1
    assert Path(keep["clone"], "RESULT.txt").read_text() == "done by keep\n"
    under_work = [str(Path(side["clone"]).relative_to(tmp_path / "work")) for side in (keep, delegate)]
    assert not any("keep" in p or "delegate" in p for p in under_work)
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert all(c["router"] == "off" for c in calls)
    assert [c["note"] is not None for c in calls if not c["args"][c["args"].index("-p") + 1].startswith("Reply")] \
        == [name == "delegate" for name in result["order"]]


def test_cold_side_is_retried_then_inconclusive(tmp_path: Path, snap: Path, fake_claude: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_COLD", "1")
    result = replay.replay_pair(snap, tmp_path / "work", fake_claude, random.Random(1))
    assert result["inconclusive"]
    assert {s["attempt"] for s in result["sides"].values()} == {replay.ATTEMPTS}
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_jev_replay.py -q`
Expected: `AttributeError: module 'replay' has no attribute 'install_session'`.

- [ ] **Step 4: Implement** (append to `replay.py`; add `import random`, `import re`, `import time`, `import uuid`, `import usage`)

```python
DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
WARMUP = "Reply with the single word ok and do nothing else."
ATTEMPTS = 3
TIMEOUT_SECONDS = 4 * 3600


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


def install_session(snap: Path, clone: Path, claude_home: Path) -> str:
    """Copy the snapshot's conversation in as a new session of the clone.

    Every mention of the original checkout's path becomes the clone's path, so an
    agent that reuses an absolute path from the conversation edits the clone,
    never the user's real working copy."""
    meta = json.loads((snap / "meta.json").read_text())
    new = str(uuid.uuid4())
    lines = []
    for line in (snap / "transcript.jsonl").read_text().splitlines():
        line = line.replace(meta["toplevel"], str(clone))
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and "sessionId" in entry:
            entry["sessionId"] = new
        lines.append(json.dumps(entry))
    target = project_dir(claude_home, workdir(meta, clone))
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{new}.jsonl").write_text("\n".join(lines) + "\n")
    return new


def run_claude(clone: Path, session_id: str, prompt: str, env_extra: dict, claude_home: Path) -> tuple[dict, float, int]:
    """One headless fork of the session, with the same access as the user's own session."""
    cmd = [os.environ.get("JEV_FORK_CHECK_CLAUDE", "claude"), "--resume", session_id, "--fork-session", "-p", prompt, "--output-format", "json",
           "--permission-mode", "bypassPermissions"]
    env = {**os.environ, "JEV_ROUTER": "off", **env_extra}
    # Reason: setting CLAUDE_CONFIG_DIR, even to the default, moves where Claude Code
    # reads its settings and MCP servers, so it is set only for a test home.
    if claude_home != DEFAULT_CLAUDE_HOME:
        env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=clone, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    wall = time.monotonic() - started
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {}
    return (data if isinstance(data, dict) else {}), wall, proc.returncode


def measure(claude_home: Path, clone: Path, session_id: str, prompt: str, helper: str) -> dict:
    pdir = project_dir(claude_home, clone)
    path = pdir / f"{session_id}.jsonl"
    entries = usage.read_entries(path) if session_id and path.exists() else []
    starts = [i for i, e in enumerate(entries) if usage.is_typed(e) and usage.entry_text(e) == prompt.strip()]
    turn = entries[starts[-1]:] if starts else []
    main = usage.calls(turn)
    subs = [c for f in sorted((pdir / session_id / "subagents").glob("*.jsonl")) for c in
            usage.calls(usage.read_entries(f), sidechain=True)] if session_id else []
    prices = usage.load_prices()
    costs = [c.cost(prices) for c in main + subs]
    handed = any(
        b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task")
        and (b.get("input") or {}).get("subagent_type") == helper
        for e in turn if e.get("type") == "assistant"
        for b in (e.get("message") or {}).get("content") or [] if isinstance(b, dict)
    )
    return {
        "calls": len(main) + len(subs),
        "cost": sum(c for c in costs if c is not None),
        "unpriced_calls": sum(c is None for c in costs),
        "first_cache_read": main[0].read if main else 0,
        "first_context": main[0].context if main else 0,
        "delegated": handed,
    }


def replay_pair(snap: Path, work: Path, claude_home: Path, rng: random.Random) -> dict:
    """Keep and delegate, one after the other in random order, each in a fresh clone
    with a verified warm cache. A cold side is rerun in a new clone."""
    meta = json.loads((snap / "meta.json").read_text())
    prompt = (snap / "message.txt").read_text()
    order = ["keep", "delegate"]
    rng.shuffle(order)
    sides: dict[str, dict] = {}
    for position, name in enumerate(order, 1):
        side: dict = {}
        for attempt in range(1, ATTEMPTS + 1):
            # Reason: folders are named by run order, which is random, so a path
            # never tells the judge which side made a result.
            clone = restore(snap, work / f"side-{position}" / f"attempt-{attempt}")
            sid = install_session(snap, clone, claude_home)
            cwd = workdir(meta, clone)
            # Reason: the warm-up forks the same conversation with the same tools
            # and system prompt, so the side's first call can read it from cache.
            warm_data, _, _ = run_claude(cwd, sid, WARMUP, {}, claude_home)
            warm = measure(claude_home, cwd, str(warm_data.get("session_id") or ""), WARMUP, "")
            extra = {"JEV_ROUTER_NOTE_FILE": str(snap / "note.txt")} if name == "delegate" else {}
            data, wall, code = run_claude(cwd, sid, prompt, extra, claude_home)
            m = measure(claude_home, cwd, str(data.get("session_id") or ""), prompt, meta["helper"])
            is_warm = warm["calls"] > 0 and m["first_cache_read"] >= 0.99 * warm["first_context"] - 2_000
            side = {"attempt": attempt, "clone": str(clone), "session_id": data.get("session_id"),
                    "wall_seconds": round(wall, 1), "exit_code": code, "warm": is_warm, **m}
            if is_warm:
                break
        sides[name] = side
    # Reason: a call with no price would count as free and flatter one side.
    comparable = all(s["warm"] and not s["unpriced_calls"] for s in sides.values())
    return {"id": meta["id"], "order": order, "sides": sides, "inconclusive": not comparable}
```

- [ ] **Step 5: Add `replay` to `fork_check.py`**

```python
def cmd_replay(sid: str | None, trial: bool) -> int:
    import random

    import replay

    done = statuses()
    if sid is None:
        todo = [p.name for p in snapshot_dirs() if done.get(p.name, {}).get("status") == "safe"]
        if not todo:
            print("No snapshot is marked safe and waiting. Mark one with: mark ID safe")
            return 1
        sid = todo[0]
    elif done.get(sid, {}).get("status") != "safe":
        print(f"{sid} is not marked safe; mark it first.")
        return 1
    snap, out = root() / "snapshots" / sid, root() / "results" / sid
    try:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        claude_home = Path(configured) if configured else replay.DEFAULT_CLAUDE_HOME
        result = replay.replay_pair(snap, out, claude_home, random.Random())
    except replay.Inconclusive as exc:
        set_status(sid, "inconclusive", str(exc))
        print(f"{sid}: inconclusive ({exc})")
        return 0
    result["trial"] = trial
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    reason = "never warm, or a call had no price" if result["inconclusive"] else ""
    set_status(sid, "inconclusive" if result["inconclusive"] else "replayed", reason)
    for name in result["order"]:
        s = result["sides"][name]
        print(f"{name}: ${s['cost']:.2f}, {s['wall_seconds']:.0f} s, {s['calls']} calls, warm={s['warm']}"
              + ("" if name == "keep" else f", handed to the helper={s['delegated']}"))
    return 0
```

with `import os` at the top, and in `main()`:

```python
    rep = sub.add_parser("replay")
    rep.add_argument("id", nargs="?")
    rep.add_argument("--next", action="store_true")
    rep.add_argument("--trial", action="store_true")
```

dispatching `replay` to `cmd_replay(None if args.next else args.id, args.trial)`.

- [ ] **Step 6: Run**

Run: `uv run pytest tests/test_jev_replay.py -q`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add jev-router/skills/jev/scripts/replay.py jev-router/skills/jev/scripts/fork_check.py tests/fake_claude.py tests/test_jev_replay.py
git commit -m "jev-router: replay each snapshot both ways with a verified warm cache"
```

---

### Task 12: Publish both results to a private copy with a comparison PR

**Files:**
- Create: `jev-router/skills/jev/scripts/publish.py`
- Modify: `jev-router/skills/jev/scripts/fork_check.py` (`publish`)
- Test: `tests/test_jev_publish.py`

**Interfaces:**
- Consumes: `results/<id>/result.json` (Task 11), snapshot `meta.json`.
- Produces: `publish.copy_name(origin_url: str, owner: str) -> str`; `publish.publish(snap: Path, result: dict, *, gh: Callable[..., str] = run_gh, remote: str | None = None) -> str` (the PR URL); branches `replay/<id>/keep`, `replay/<id>/delegate`, `replay/<id>/compare`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_publish.py`)

```python
"""Results go to a private copy, with a PR that shows exactly keep versus delegate."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import publish  # noqa: E402
import replay  # noqa: E402
from test_jev_replay import snap  # noqa: E402, F401
from test_jev_snapshot import git, repo  # noqa: E402, F401


def test_an_existing_public_copy_stops_everything(tmp_path: Path, repo: Path, snap: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")

    def gh(*args: str) -> str:
        if args[:2] == ("api", "user"):
            return "yorrick\n"
        if args[:2] == ("repo", "view"):
            return "PUBLIC\n"
        raise AssertionError(f"unexpected gh call: {args}")

    result = {"id": "x", "sides": {"keep": {"clone": str(tmp_path / "k")}, "delegate": {"clone": str(tmp_path / "d")}}}
    with pytest.raises(RuntimeError, match="not private"):
        publish.publish(snap, result, gh=gh, remote=str(tmp_path / "copy.git"))


def test_copy_name_is_private_repo_named_after_the_source() -> None:
    assert publish.copy_name("git@github.com:acme/shop.git", "yorrick") == "yorrick/shop-jev-replays"
    assert publish.copy_name("https://github.com/acme/shop", "yorrick") == "yorrick/shop-jev-replays"


def test_publish_pushes_both_results_and_opens_the_compare_pr(tmp_path: Path, repo: Path, snap: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    (keep / "RESULT.txt").write_text("keep\n")
    (delegate / "RESULT.txt").write_text("delegate\n")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    calls: list[tuple[str, ...]] = []

    def gh(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("api", "user"):
            return "yorrick\n"
        if args[:2] == ("repo", "view"):
            raise RuntimeError("not found")
        return "https://github.com/yorrick/shop-jev-replays/pull/1\n" if args[:2] == ("pr", "create") else ""

    sid = json.loads((snap / "meta.json").read_text())["id"]
    result = {"id": sid, "sides": {"keep": {"clone": str(keep), "cost": 1.0, "wall_seconds": 60, "calls": 10,
              "delegated": False}, "delegate": {"clone": str(delegate), "cost": 0.5, "wall_seconds": 50, "calls": 12,
              "delegated": True}}}
    url = publish.publish(snap, result, gh=gh, remote=str(remote))
    assert url == "https://github.com/yorrick/shop-jev-replays/pull/1"
    assert ("repo", "create", "yorrick/shop-jev-replays", "--private", "--description",
            "jev-router fork-check replays") in calls
    shown = git(remote, "show", f"replay/{sid}/compare:RESULT.txt")
    assert shown == "delegate\n"
    assert git(remote, "rev-parse", f"replay/{sid}/compare^") == git(remote, "rev-parse", f"replay/{sid}/keep")
    for side in ("keep", "delegate", "compare"):
        assert ".env" not in git(remote, "ls-tree", "-r", "--name-only", f"replay/{sid}/{side}").split()
    assert (keep / "RESULT.txt").read_text() == "keep\n"  # the clone's files are untouched
    pr = next(c for c in calls if c[:2] == ("pr", "create"))
    assert pr[pr.index("--base") + 1] == f"replay/{sid}/keep"
    assert pr[pr.index("--head") + 1] == f"replay/{sid}/compare"
    assert chr(0x2014) not in pr[pr.index("--body") + 1]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_publish.py -q`
Expected: `ModuleNotFoundError: No module named 'publish'`.

- [ ] **Step 3: Implement `publish.py`**

```python
"""Push a replay's two results to a private copy of the repository and open a PR
that shows the delegate result against the keep result."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import IDENTITY, run


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
    # Reason: `add -A` never adds ignored files, so the copied .env and
    # dependencies stay out of every published commit.
    run("git", "-C", str(clone), "add", "-A")
    run("git", "-C", str(clone), *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)


def publish(snap: Path, result: dict, *, gh: Callable[..., str] = run_gh, remote: str | None = None) -> str:
    meta = json.loads((snap / "meta.json").read_text())
    sid = result["id"]
    origin = run("git", "-C", meta["toplevel"], "remote", "get-url", "origin").strip()
    copy = copy_name(origin, gh("api", "user", "--jq", ".login").strip())
    try:
        visibility = gh("repo", "view", copy, "--json", "visibility", "--jq", ".visibility").strip()
    except RuntimeError:
        gh("repo", "create", copy, "--private", "--description", "jev-router fork-check replays")
        visibility = "PRIVATE"
    if visibility != "PRIVATE":
        raise RuntimeError(f"{copy} exists and is not private, so nothing was pushed")
    url = remote or f"git@github.com:{copy}.git"
    keep, delegate = Path(result["sides"]["keep"]["clone"]), Path(result["sides"]["delegate"]["clone"])
    for side, clone in (("keep", keep), ("delegate", delegate)):
        commit_all(clone, f"jev fork check {sid}: result")
        run("git", "-C", str(clone), "push", "-q", url, f"HEAD:refs/heads/replay/{sid}/{side}")
    # The comparison commit is built without checking anything out, so neither
    # clone's files change after the replay.
    run("git", "-C", str(keep), "fetch", "-q", str(delegate), "HEAD")
    tree = run("git", "-C", str(keep), "rev-parse", "FETCH_HEAD^{tree}").strip()
    compare = run(
        "git", "-C", str(keep), *IDENTITY, "commit-tree", tree, "-p", "HEAD", "-m", f"jev fork check {sid}: comparison"
    ).strip()
    run("git", "-C", str(keep), "push", "-q", url, f"{compare}:refs/heads/replay/{sid}/compare")
    k, d = result["sides"]["keep"], result["sides"]["delegate"]
    body = (
        f"Fork check {sid}. The base branch holds the keep result; this pull request shows the delegate result "
        f"against it.\n\nKeep: ${k['cost']:.2f}, {k['wall_seconds']:.0f} s, {k['calls']} calls. "
        f"Delegate: ${d['cost']:.2f}, {d['wall_seconds']:.0f} s, {d['calls']} calls"
        f"{'' if d['delegated'] else ', and the session kept the job instead of handing it off'}.\n\n"
        "The job:\n\n" + "\n".join(f"> {line}" for line in (snap / "message.txt").read_text().splitlines())
    )
    return gh("pr", "create", "--repo", copy, "--base", f"replay/{sid}/keep", "--head", f"replay/{sid}/compare",
              "--title", f"{sid}: keep vs delegate", "--body", body).strip()
```

- [ ] **Step 4: Add `publish` to `fork_check.py`**

```python
def cmd_publish(sid: str) -> int:
    import publish

    if not (root() / "results" / sid / "verdict.json").exists():
        print(f"Judge {sid} first: the judge must see the results before anything is committed or pushed.")
        return 1
    result = json.loads((root() / "results" / sid / "result.json").read_text())
    url = publish.publish(root() / "snapshots" / sid, result)
    set_status(sid, "published", url)
    print(url)
    return 0
```

registered as `sub.add_parser("publish").add_argument("id")`.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_jev_publish.py -q`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add jev-router/skills/jev/scripts/publish.py jev-router/skills/jev/scripts/fork_check.py tests/test_jev_publish.py
git commit -m "jev-router: publish replay results to a private copy with a compare PR"
```

---

### Task 13: The blind judge and the fork-check report

**Files:**
- Create: `jev-router/skills/jev/scripts/judge.py`
- Modify: `jev-router/skills/jev/scripts/report.py`, `jev-router/skills/jev/scripts/fork_check.py` (`judge`, `report`)
- Test: `tests/test_jev_judge.py`

**Interfaces:**
- Consumes: `result.json`, `refs/jev/start` in each clone, version-3 log events.
- Produces: `judge.parse(text: str) -> dict`; `judge.judge(snap: Path, result: dict, work: Path, *, codex: Callable[[str, Path], str] = run_codex, rng: random.Random) -> dict` (keys `keep{tests,outcome_met}`, `delegate{...}`, `prefer` in `keep|delegate|tie`, `why`), written to `verdict.json`; `report.check_report(points: list[dict], events: list[dict], skipped: int, inconclusive: int, period_cost: float | None = None, waiting: tuple[str, ...] = ()) -> tuple[str, bool]` (`waiting`: among the first 20 eligible jobs, those not yet replayed or judged) where each point is `{"meta": ..., "result": ..., "verdict": ...}`.

- [ ] **Step 1: Write the failing tests** (`tests/test_jev_judge.py`)

```python
"""A blind judge, and the fork check's pass or fail."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import judge  # noqa: E402
import replay  # noqa: E402
import report  # noqa: E402
from test_jev_replay import snap  # noqa: E402, F401
from test_jev_snapshot import repo  # noqa: E402, F401

ANSWER = 'Both fine.\n{"A": {"tests": "pass", "outcome_met": true}, "B": {"tests": "fail", "outcome_met": false}, ' \
         '"prefer": "A", "why": "B broke a test."}'


def test_parse_takes_the_last_json_line() -> None:
    assert judge.parse(ANSWER)["prefer"] == "A"


def test_labels_are_mapped_back_to_keep_and_delegate(tmp_path: Path, snap: Path) -> None:
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    seen: dict = {}

    def codex(prompt: str, work: Path) -> str:
        seen["prompt"], seen["dirs"] = prompt, sorted(p.name for p in work.iterdir())
        return ANSWER

    result = {"sides": {"keep": {"clone": str(keep)}, "delegate": {"clone": str(delegate)}}}
    rng = random.Random(3)
    labels = ["keep", "delegate"]
    random.Random(3).shuffle(labels)  # the same draw judge() makes: A gets labels[0]
    verdict = judge.judge(snap, result, tmp_path / "j", codex=codex, rng=rng)
    assert verdict[labels[0]] == {"tests": "pass", "outcome_met": True}
    assert verdict["prefer"] == labels[0]
    assert "LEAF" in seen["prompt"] and "refs/jev/start" in seen["prompt"]
    assert seen["dirs"] == ["A", "B"]
    assert subprocess.run(["git", "-C", str(tmp_path / "j" / "A"), "remote"], capture_output=True,
                          text=True).stdout == ""


def point(keep_cost: float, del_cost: float, prefer: str = "tie", delegate_ok: bool = True) -> dict:
    side = {"tests": "pass", "outcome_met": True}
    return {
        "meta": {"id": "x", "created": "2026-10-01T10:00:00+00:00", "median_calls": 20},
        "result": {"sides": {"keep": {"cost": keep_cost, "wall_seconds": 100, "calls": 20, "delegated": False},
                             "delegate": {"cost": del_cost, "wall_seconds": 90, "calls": 22, "delegated": True}}},
        "verdict": {"keep": side, "delegate": side if delegate_ok else {"tests": "fail", "outcome_met": True},
                    "prefer": prefer, "why": ""},
    }


def test_check_passes_on_twenty_cheaper_equal_jobs() -> None:
    text, passed = report.check_report([point(1.0, 0.8)] * 20, [], skipped=2, inconclusive=1)
    assert passed
    assert "Skipped by you: 2. Inconclusive: 1." in text


def test_a_job_waiting_for_its_verdict_blocks_the_pass() -> None:
    text, passed = report.check_report([point(1.0, 0.8)] * 19, [], 0, 0, waiting=("20261001-120000-abc",))
    assert not passed
    assert "waiting for 20261001-120000-abc" in text


def test_one_broken_delegate_result_fails_the_check() -> None:
    _, passed = report.check_report([point(1.0, 0.5)] * 19 + [point(1.0, 0.5, delegate_ok=False)], [], 0, 0)
    assert not passed


def test_period_share_is_reported_without_a_threshold() -> None:
    text, _ = report.check_report([point(1.0, 0.8)] * 20, [], 0, 0, period_cost=40.0)
    assert "Saving as a share of all Claude Code cost over the period: 10%" in text


def test_jev_overhead_counts_against_delegation() -> None:
    events = [{"version": 3, "ts": "2026-10-01T10:00:00+00:00", "cost": 1.5, "latency_ms": 0}]
    _, passed = report.check_report([point(1.0, 0.85)] * 20, events, 0, 0)
    assert not passed  # 17.0 + 1.5 > 0.9 * 20
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_jev_judge.py -q`
Expected: `ModuleNotFoundError: No module named 'judge'`.

- [ ] **Step 3: Implement `judge.py`**

```python
"""A blind judge: Codex compares the two results without knowing which side made which."""

from __future__ import annotations

import json
import random
import subprocess
from collections.abc import Callable
from pathlib import Path

from replay import copy_tree, run

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
You do not know which agent made which, and the order is random. The job:

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
        ["codex", "exec", "-m", "gpt-6-sol", "-c", "model_reasoning_effort=max", "--disable", "hooks",
         "--sandbox", "workspace-write", "--skip-git-repo-check", "-C", str(work), "-o", str(out), "-"],
        input=prompt, text=True, capture_output=True, timeout=2 * 3600, check=True,
    )
    return out.read_text()


def parse(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and {"A", "B", "prefer"} <= data.keys():
            return data
    raise ValueError("the judge gave no verdict line")


def judge(snap: Path, result: dict, work: Path, *, codex: Callable[[str, Path], str] = run_codex,
          rng: random.Random) -> dict:
    labels = ["keep", "delegate"]
    rng.shuffle(labels)
    names = dict(zip(("A", "B"), labels, strict=True))
    work.mkdir(parents=True, exist_ok=True)
    for letter, side in names.items():
        copy_tree(Path(result["sides"][side]["clone"]), work / letter)
        # Reason: the origin's path would say which replay folder this came from.
        run("git", "-C", str(work / letter), "remote", "remove", "origin")
    raw = parse(codex(PROMPT.format(message=(snap / "message.txt").read_text(), example=EXAMPLE), work))
    verdict = {
        names["A"]: {"tests": raw["A"]["tests"], "outcome_met": bool(raw["A"]["outcome_met"])},
        names["B"]: {"tests": raw["B"]["tests"], "outcome_met": bool(raw["B"]["outcome_met"])},
        "prefer": names.get(raw["prefer"], "tie"),
        "why": str(raw.get("why", "")),
    }
    (work / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict
```

- [ ] **Step 4: Add the fork-check report to `report.py`**

```python
POINTS = 20


def _broken(verdict: dict) -> bool:
    """A delegated result that fails a test or its goal where the kept one passes."""
    keep, delegate = verdict["keep"], verdict["delegate"]
    return (delegate["tests"] == "fail" and keep["tests"] != "fail") or (
        not delegate["outcome_met"] and keep["outcome_met"]
    )


def check_report(
    points: list[dict],
    events: list[dict],
    skipped: int,
    inconclusive: int,
    period_cost: float | None = None,
    waiting: tuple[str, ...] = (),
) -> tuple[str, bool]:
    points = points[:POINTS]
    keep_cost = sum(p["result"]["sides"]["keep"]["cost"] for p in points)
    del_cost = sum(p["result"]["sides"]["delegate"]["cost"] for p in points)
    keep_time = sum(p["result"]["sides"]["keep"]["wall_seconds"] for p in points)
    del_time = sum(p["result"]["sides"]["delegate"]["wall_seconds"] for p in points)
    period = period_events(events, points)
    del_cost += sum(e.get("cost") or 0 for e in period)
    del_time += sum(e.get("latency_ms") or 0 for e in period) / 1000
    broken = [p for p in points if _broken(p["verdict"])]
    prefer_keep = sum(p["verdict"]["prefer"] == "keep" for p in points)
    prefer_delegate = sum(p["verdict"]["prefer"] == "delegate" for p in points)
    overrides = sum(not p["result"]["sides"]["delegate"]["delegated"] for p in points)
    conditions = {
        f"{POINTS} jobs, all judged": len(points) == POINTS and not waiting,
        "at least 10% cheaper": bool(points) and del_cost <= 0.9 * keep_cost,
        "as good": not broken and prefer_keep <= prefer_delegate,
        "not slower": bool(points) and del_time <= keep_time,
    }
    lines = [
        "# Fork check",
        "",
        f"Jobs judged: {len(points)} of {POINTS}"
        + (f", waiting for {', '.join(waiting)}" if waiting else "")
        + f". Skipped by you: {skipped}. Inconclusive: {inconclusive}. "
        f"Overrides (the session kept a job it was told to hand off): {overrides}.",
        f"Cost: keep ${keep_cost:.2f}, delegate ${del_cost:.2f} with Jev's cost over the period included.",
        f"Time: keep {keep_time / 60:.0f} min, delegate {del_time / 60:.0f} min with Jev's added wait included.",
        f"Blind judge: prefers keep {prefer_keep}, delegate {prefer_delegate}; broken delegated results: {len(broken)}.",
        *(
            [f"Saving as a share of all Claude Code cost over the period: {(keep_cost - del_cost) / period_cost:.0%} "
             "(no threshold: it depends on how the work mixes long and short jobs)."]
            if period_cost
            else []
        ),
        "",
        *[f"- {name}: {'pass' if ok else 'FAIL'}" for name, ok in conditions.items()],
        "",
        "| job | predicted calls | calls keep / delegate | cost keep / delegate | minutes keep / delegate | judge |",
        "|---|---|---|---|---|---|",
    ]
    for p in points:
        k, d = p["result"]["sides"]["keep"], p["result"]["sides"]["delegate"]
        lines.append(
            f"| {p['meta']['id']} | {p['meta'].get('median_calls')} | {k['calls']} / {d['calls']} | "
            f"${k['cost']:.2f} / ${d['cost']:.2f} | {k['wall_seconds'] / 60:.0f} / {d['wall_seconds'] / 60:.0f} | "
            f"{p['verdict']['prefer']} |"
        )
    return "\n".join(lines), all(conditions.values())
```

- [ ] **Step 5: Add `judge` and `report` to `fork_check.py`**

```python
def cmd_judge(sid: str) -> int:
    import random

    import judge

    out = root() / "results" / sid
    verdict = judge.judge(root() / "snapshots" / sid, json.loads((out / "result.json").read_text()), out / "judge",
                          rng=random.Random())
    (out / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    set_status(sid, "judged", verdict["prefer"])
    print(json.dumps(verdict, indent=2))
    return 0


def cmd_report() -> int:
    """The first 20 jobs marked safe, in capture order, whose replay is conclusive:
    a later job never stands in for an earlier one that is not judged yet."""
    done = statuses()
    points: list[dict] = []
    waiting: list[str] = []
    for snap in snapshot_dirs():
        if done.get(snap.name, {}).get("status") not in ("safe", "replayed", "published", "judged"):
            continue
        out = root() / "results" / snap.name
        result = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else None
        if result is not None and (result.get("trial") or result.get("inconclusive")):
            continue
        if len(points) + len(waiting) == report.POINTS:
            break
        if result is None or not (out / "verdict.json").exists():
            waiting.append(snap.name)
            continue
        points.append({"meta": json.loads((snap / "meta.json").read_text()), "result": result,
                       "verdict": json.loads((out / "verdict.json").read_text())})
    skipped = sum(s["status"] == "skip" for s in done.values())
    inconclusive = sum(s["status"] == "inconclusive" for s in done.values())
    events = jev_router.read_log()
    period = report.period_events(events, points)
    period_cost = sum(r["real_cost"] for r in report.shadow_rows(period, usage.load_prices())) if period else None
    text, passed = report.check_report(points, events, skipped, inconclusive, period_cost, tuple(waiting))
    (root() / "report.md").write_text(text + "\n")
    print(text)
    print("\nThe fork check PASSES." if passed else "\nThe fork check has not passed (yet).")
    return 0
```

registered as `sub.add_parser("judge").add_argument("id")` and `sub.add_parser("report")`.

- [ ] **Step 6: Run everything**

Run: `uv run pytest tests/ -q && uv run ruff check scripts/ tests/ jev-router/ && uv run ruff format --check . && uv run pyright`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add jev-router/skills/jev/scripts/judge.py jev-router/skills/jev/scripts/report.py jev-router/skills/jev/scripts/fork_check.py tests/test_jev_judge.py
git commit -m "jev-router: blind Codex judge and the fork-check report"
```

---

### Task 14: Docs, version 0.3.0, review, merge and install

**Files:**
- Modify: `jev-router/plugin.toml` (version `0.3.0`, description), `jev-router/README.md`, `jev-router/skills/jev/SKILL.md`, `README.md` (plugin row), generated manifests
- Modify: `docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md` only if the implementation had to differ beyond what this plan already changed in it (say where and why)

- [ ] **Step 1: Update the docs**

`plugin.toml`: `version = "0.3.0"`, description: "Asks Jev (via OpenRouter) how long each job will run and, in Claude Code, whether a fresh subagent would do it cheaper. Starts in shadow mode. Off until you turn it on with /jev on."

`SKILL.md`: add `/jev mode shadow|capture|live` (runs `jev_router.py mode ...`) and a short "Fork check" section naming the `fork_check.py` commands in order (`check`, `list`, `mark`, `replay`, `judge`, `publish`, `report`, `shadow`).

`jev-router/README.md`: a section "Claude Code: cache-aware delegation (0.3)" of at most three short paragraphs: what it decides and why (re-reading the context is the cost), the three modes, and the fork check in one paragraph with the command order. Link the spec. Keep the Codex and opencode sections as they are (they still describe 0.2.0 behaviour). No em dashes.

- [ ] **Step 2: Regenerate and run the full checklist**

```bash
uv run scripts/sync_manifests.py
uv run scripts/sync_manifests.py --check
uv run scripts/validate_skills.py
uv run pytest tests/ -q
uv run ruff check scripts/ tests/ jev-router/
uv run ruff format --check .
uv run pyright
uv run python -c "import pathlib; files = [*pathlib.Path('jev-router').rglob('*'), pathlib.Path('docs/superpowers/plans/2026-09-29-jev-router-cache-aware.md')]; bad = [str(f) for f in files if f.is_file() and chr(0x2014) in f.read_text(errors='ignore')]; print(bad or 'no em dashes')"
```

Expected: every command passes; the last prints `no em dashes`.

- [ ] **Step 3: Commit**

```bash
git add -A jev-router/ README.md .claude-plugin/ .agents/ package.json
git commit -m "jev-router 0.3.0: cache-aware delegation for Claude Code, shadow mode and the fork check"
```

- [ ] **Step 4: Cross-AI review of the implementation**

Write a payload (spec path, plan path, `git diff main...HEAD --stat`, the Review Focus list, "you are a LEAF reviewer") to `~/work/data/jev-router-study/impl_review1_prompt.md` and run:

```bash
cat ~/work/data/jev-router-study/impl_review1_prompt.md | codex exec -m gpt-6-sol -c model_reasoning_effort=max --disable hooks --sandbox read-only --skip-git-repo-check -o ~/work/data/jev-router-study/impl_review1_out.md -
```

Verify each finding against the code, fix the real ones with a test each, commit, and repeat until the reviewer ends with "approve".

- [ ] **Step 5: Push and open the PR (part of this approved plan; merging needs the user's explicit go-ahead)**

```bash
git push origin HEAD
gh pr create --title "jev-router 0.3.0: cache-aware delegation for Claude Code" --body "In Claude Code the router now predicts how long a job will run and whether a fresh subagent would do it cheaper, and starts in shadow mode, so nothing changes for the session until the fork check passes. The fork-check runner replays captured jobs both ways and reports cost, time and a blind Codex verdict. Codex and opencode keep 0.2.0's behaviour.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

(If `gh` triggers a keychain dialog, it is safe to Deny.) After CI passes and the user approves the merge: `gh pr merge --squash --admin` (no `--delete-branch` from a worktree; delete the remote branch with `gh api -X DELETE repos/yorrick/agent-skills/git/refs/heads/<branch>`).

- [ ] **Step 6: Update every harness on this machine and verify**

```bash
claude plugin marketplace update yorrick && claude plugin update jev-router@yorrick
codex plugin marketplace upgrade
rm -rf ~/.cache/opencode/packages/yorrick-agent-skills*
claude plugin list | grep -A2 jev-router
ls ~/.codex/plugins/cache/yorrick/jev-router/
```

Expected: Claude Code and Codex both report `0.3.0`. Tell the user to run `/reload-plugins` in open Claude Code sessions.

---

### Task 15: Trial run (with the user)

This task proves the whole chain on one or two real jobs before days of capture (spec, Validation step 0). Nothing here counts toward the 20.

- [ ] **Step 1: Switch Claude Code to capture**

```bash
S=$(ls -d ~/.claude/plugins/cache/yorrick/jev-router/0.3.0/skills/jev/scripts)
uv run --script $S/jev_router.py mode capture --dir ~/work/data/jev-fork-check
uv run --script $S/jev_router.py status
```

Expected: "mode capture" and the capture directory. The user reloads plugins and works as usual.

- [ ] **Step 2: Wait for one or two snapshots, then restore-check them**

```bash
uv run --script $S/fork_check.py shadow
uv run --script $S/fork_check.py check
uv run --script $S/fork_check.py list
```

Expected: `shadow` shows messages decided and a few worth a fresh subagent (the study predicts about 9% of typed messages); `check` prints `restore_ok`; `list` shows the snapshot with its external actions. If nothing is selected after a day of normal work, report the shadow numbers to the user before changing anything.

- [ ] **Step 3: The user marks one safe; replay it as a trial**

```bash
git -C <snapshot toplevel> status --porcelain > ~/work/data/jev-fork-check/trial-before.txt
uv run --script $S/fork_check.py mark <ID> safe
uv run --script $S/fork_check.py replay <ID> --trial
git -C <snapshot toplevel> status --porcelain | diff - ~/work/data/jev-fork-check/trial-before.txt && echo "real checkout untouched"
```

Expected: both sides `warm=True`; the delegate side `handed to the helper=True` (or an override, which is a finding to report, not a bug); non-zero costs; `real checkout untouched`. Also confirm the first cache read by opening `results/<ID>/result.json` (`first_cache_read` close to the warm-up's `first_context`). If the sides are never warm, stop and investigate the warm-up with systematic debugging before anything else: the comparison is invalid without it.

- [ ] **Step 4: Publish, judge and report**

```bash
uv run --script $S/fork_check.py judge <ID>
uv run --script $S/fork_check.py publish <ID>
uv run --script $S/fork_check.py report
```

Expected: a verdict with `prefer` (the judge runs first, on untouched results); a PR URL on a private `<owner>/<repo>-jev-replays` whose diff is exactly keep versus delegate and holds no `.env`; a report with 0 of 20 jobs judged (the trial is excluded).

- [ ] **Step 5: Fix what the trial found, then hand over**

Fix each problem with a failing test first, rerun the trial on a new snapshot if a fix touched capture or replay, and then tell the user: capture is running, how many jobs are waiting to be marked (`list`), and that `report` shows progress toward 20.
