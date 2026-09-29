#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Jev router: size every message with Jev and send small jobs to a smaller model.

One script serves Claude Code, Codex and opencode. Each harness's per-message
hook runs `hook <harness>` with the user's message as JSON on stdin. The user
flips the router with `on` and `off`, reads the counters with `status`, and can
size messages by hand with `classify`.

Codex and opencode hand a job to the model Jev sizes it for. Claude Code instead
asks Jev how many model calls the job takes and prices keeping it in the session
(every call re-reads the whole conversation) against briefing a fresh subagent;
`mode` sets whether it only logs that decision (`shadow`, `capture`) or tells
the session (`live`).

The router must never get in the way of a message. When it is off, when Jev is
slow, or when anything at all fails, `hook` prints nothing and exits 0, which
every harness treats as "no opinion": the message goes through unchanged.

Standard library only, on purpose: the hook runs before every message, and a
dependency would add resolver time to each one.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

# Reason: sibling modules, found because `uv run --script` puts this folder first on sys.path.
import delegation
import session as sessions
import snapshot
import usage

# Reason: Jev itself, through OpenRouter's Decisions API. Not `typesafe/jev-router`,
# OpenRouter's chat router built on Jev: that one forwards the prompt to whichever
# chat model it picks and returns that model's text, so it took about 3 s and
# failed with upstream 502/504s, and its "confidence" was that model's own guess.
# Jev answers a typed question with real probabilities in about 0.3 s.
API_URL = os.environ.get("JEV_ROUTER_API_URL", "https://openrouter.ai/api/alpha/decisions")
# Pinned, not `~typesafe/jev-latest`: the 60% threshold is tuned against one version.
JEV_MODEL = "typesafe/jev-1.13"
MIN_CONFIDENCE = 60
# Reason: a follow-up is "more likely than not" a reply that needs the conversation.
FOLLOW_UP_AT = 0.5
# Reason: Jev answered in 0.16-0.5 s across 24 probe calls; anything slower is an
# outage, and the message should not wait on it.
DEFAULT_TIMEOUT_SECONDS = 2.0
# Reason: Jev only needs the gist to size a job; a long paste would cost more and
# answer slower without changing the verdict.
MAX_MESSAGE_CHARS = 4000
HARNESSES = ("claude", "codex", "opencode")


@dataclass(frozen=True)
class Tier:
    """One size Jev can pick, and the model and thinking level that take it."""

    size: str
    jobs: str
    model: str
    model_id: str
    effort: str | None
    helper: str

    @property
    def label(self) -> str:
        """How the tier reads to a person, e.g. 'Claude Opus 5.5 at low thinking'."""
        return f"{self.model} at {self.effort} thinking" if self.effort else self.model


def load_tiers() -> tuple[dict[str, str], dict[str, tuple[Tier, ...]]]:
    """Read tiers.json, which the opencode hook reads too.

    Each size maps to a model AND a thinking level, chosen from Artificial
    Analysis's Intelligence Index and cost per task: a setting that another one
    beats on both is left out. Every model belongs to the harness it is listed
    under, because the router only ever routes inside one harness. A Codex plugin
    cannot ship agent definitions, but spawn_agent takes a model and a reasoning
    effort directly, so a Codex helper is defined at the moment it is spawned.
    """
    data = json.loads((Path(__file__).with_name("tiers.json")).read_text())
    jobs: dict[str, str] = data["jobs"]
    tiers = {
        harness: tuple(
            Tier(
                size=t["size"],
                jobs=jobs[t["size"]],
                model=t["model"],
                model_id=t["model_id"],
                effort=t["effort"],
                helper=t["helper"],
            )
            for t in entries
        )
        for harness, entries in data["harnesses"].items()
    }
    return jobs, tiers


JOBS, TIERS = load_tiers()
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


PRIVACY = (
    "While it is on, the text of every message you send also goes to OpenRouter and to TypeSafe "
    "(the company that makes Jev), and in Claude Code so does the agent's previous reply (its last "
    "1,500 characters). Keep it off for private work."
)


# --- state -------------------------------------------------------------------


def home() -> Path:
    """Where the switch and the log live. Shared by all three harnesses on purpose:
    one switch turns the router on or off everywhere."""
    return Path(os.environ.get("JEV_ROUTER_HOME") or Path.home() / ".config" / "jev-router")


def load_config() -> dict:
    path = home() / "config.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_config(config: dict) -> None:
    path = home() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(path)


def fork_check_dir(config: dict) -> Path:
    """Where capture mode saves snapshots (set with `mode capture --dir`)."""
    return Path(config.get("fork_check_dir") or home() / "fork-check")


def record(event: dict) -> None:
    path = home() / "log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"), **event})
    with path.open("a") as log:
        log.write(line + "\n")


def read_log() -> list[dict]:
    path = home() / "log.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


KEY_LINE = re.compile(r"""^\s*(?:export\s+|set\s+-g?x\s+)?OPENROUTER_API_KEY[=\s]\s*["']?([^"'\s]+)""")


def read_key(key_file: str | None) -> str | None:
    """The OpenRouter key from the shell file named at `on` (`export OPENROUTER_API_KEY=...`)."""
    if not key_file or not Path(key_file).is_file():
        return None
    for line in Path(key_file).read_text().splitlines():
        if match := KEY_LINE.match(line):
            return match.group(1)
    return None


# --- asking Jev ----------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    size: str
    probability: float  # the probability Jev gives its pick, from 0 to 1
    follow_up: bool

    @property
    def confidence(self) -> float:
        """The same probability in percent, unrounded, for the log."""
        return self.probability * 100

    @property
    def sure(self) -> str:
        """The percentage as people read it: 0.596 shows as 59.6. A probability
        under 60% never shows as 60, since it is kept for being under 60%."""
        text = f"{self.confidence:g}"
        if self.probability < MIN_CONFIDENCE / 100 <= float(text) / 100:
            return f"just under {MIN_CONFIDENCE}"
        return text


def jev_questions(tiers: tuple[Tier, ...]) -> dict:
    """The two typed questions Jev answers about each message, in one call."""
    return {
        "size": {
            "type": "choice",
            "instructions": "What is the smallest AI model size that can do the job in this message well?",
            "criteria": {t.size: t.jobs for t in tiers},
        },
        "follow_up": {
            "type": "noul",
            "instructions": "Does this message only make sense as a reply inside an ongoing conversation?",
            "criteria": {
                "true": "It is a short reply that refers to earlier messages, like 'yes do that but make it "
                "shorter' or 'retry'.",
                "false": "It is a request that someone who has not seen the conversation could act on.",
            },
        },
    }


class BadAnswer(ValueError):
    """Jev answered, but not with a usable verdict.

    The messages are fixed strings on purpose: whatever an error says ends up in
    the log, and the log keeps no string from a response. From a response it keeps
    only the cost and, once validated, a known size name and a probability.
    """


class NoKey(RuntimeError):
    pass


def probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise BadAnswer("a probability is not a number from 0 to 1")
    return float(value)


def parse_verdict(body: object, tiers: tuple[Tier, ...]) -> Verdict:
    """Accept only a complete verdict; anything doubtful means "carry on".

    How sure Jev is of a size is the probability it gives that size. Jev's own
    `confidence` field is something else: how concentrated the whole
    distribution is, rescaled so that an even split reads 0.
    """
    try:
        size_answer = body["answers"]["size"]  # type: ignore[index]
        follow_answer = body["answers"]["follow_up"]  # type: ignore[index]
        size = size_answer["choice"]
        picked = size_answer["probabilities"][size]
        follow_up = follow_answer["noul"]
    except (KeyError, IndexError, TypeError):
        raise BadAnswer("no size or follow-up answer") from None
    if size_answer.get("type") != "choice" or follow_answer.get("type") != "noul":
        raise BadAnswer("an answer has the wrong type")
    if size not in {t.size for t in tiers}:
        raise BadAnswer("unknown size")
    return Verdict(size=size, probability=probability(picked), follow_up=probability(follow_up) >= FOLLOW_UP_AT)


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


def cost_of(body: object) -> float | None:
    """What OpenRouter billed for the call, kept even when the answer is unusable."""
    usage = body.get("usage") if isinstance(body, dict) else None
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return cost if isinstance(cost, int | float) and not isinstance(cost, bool) else None


def timeout_of(config: dict) -> float:
    """The Jev deadline. It can only be lowered: each harness stops the whole hook
    at 8 s, and startup plus logging need the rest, so a longer wait would be cut
    off before the timeout is even recorded."""
    return min(float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)), DEFAULT_TIMEOUT_SECONDS)


def error_label(exc: Exception) -> str:
    """A log-safe description: never the text of a response."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, BadAnswer | NoKey):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def ask_jev(
    message: str,
    tiers: tuple[Tier, ...],
    key: str,
    timeout: float,
    *,
    state: dict | None = None,
    questions: dict | None = None,
) -> dict:
    """One call to Jev, abandoned at `timeout` seconds of wall-clock time. By default
    it asks the 0.2.0 questions about the message alone; the cache-aware path passes
    its own state and questions.

    urlopen's own timeout bounds each socket operation, not the whole exchange, so a
    slow trickle could outlast it; the daemon thread gives a hard overall deadline.
    """
    body = json.dumps(
        {
            "model": JEV_MODEL,
            "state": state or {"message": message[:MAX_MESSAGE_CHARS]},
            "questions": questions or jev_questions(tiers),
            # Reason: the message goes to TypeSafe and nowhere else, even if
            # OpenRouter adds another provider for Jev later. Verified: OpenRouter
            # answers 404 rather than fall back when the allowed provider is absent.
            "provider": {"only": ["typesafe"], "allow_fallbacks": False},
        }
    ).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "X-Title": "jev-router"},
    )
    outcome: dict = {}

    def call() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                outcome["body"] = json.load(response)
        except BaseException as exc:  # handed back to the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(timeout)
    error = outcome.get("error")
    if worker.is_alive() or (isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)):
        raise TimeoutError(f"Jev took longer than {timeout:g}s")
    if error is not None:
        raise error
    return outcome["body"]


# Reason: a TypeVar, not `def consult[V]`: the script declares Python 3.11, which
# cannot parse that syntax, and a syntax error would fail the hook before its guard.
V = TypeVar("V")


def consult(  # noqa: UP047
    config: dict,
    event: dict,
    prompt: str,
    tiers: tuple[Tier, ...],
    parse: Callable[[object, tuple[Tier, ...]], V],
    *,
    state: dict | None = None,
    questions: dict | None = None,
) -> V | None:
    """One guarded Jev call: the parsed verdict, or None for "no opinion".

    Fills `event` with the outcome of a failed call, the latency, and whether Jev
    answered and what that cost. Both routes use it, so both log the same way.
    """
    started = time.monotonic()
    body = verdict = None
    try:
        key = read_key(config.get("key_file"))
        if not key:
            raise NoKey("no OpenRouter API key in the key file")
        body = ask_jev(prompt, tiers, key, timeout_of(config), state=state, questions=questions)
        verdict = parse(body, tiers)
    except TimeoutError:
        event["outcome"] = "timeout"
    except Exception as exc:  # any failure means "no opinion"
        event.update(outcome="error", error=error_label(exc))
    event["latency_ms"] = round((time.monotonic() - started) * 1000)
    if body is not None:
        # Reason: no string from the response is logged: only its cost here, and
        # after validation the fields a verdict holds (known size names and numbers).
        event.update(answered=True, cost=cost_of(body))
    return verdict


def decide(verdict: Verdict, tiers: tuple[Tier, ...]) -> Tier | None:
    """The tier to hand the job to, or None when the session should keep it."""
    # Reason: compared unrounded, so 0.59999 is still under 60%.
    if verdict.follow_up or verdict.probability < MIN_CONFIDENCE / 100:
        return None
    return next(t for t in tiers if t.size == verdict.size)


def outcome_of(verdict: Verdict, tier: Tier | None) -> str:
    if tier is not None:
        return "routed"
    return "follow_up" if verdict.follow_up else "unsure"


# --- what each harness is told -------------------------------------------------


def handoff(harness: str, tier: Tier, verdict: Verdict) -> str:
    sized = f"Jev router: Jev's size for this message is {tier.size} ({verdict.sure}% sure)."
    sign_off = f"Done by {tier.label}"
    # Reason: neither hook can see the session's thinking level, but the model can
    # (verified for Claude, including after a mid-session /effort). An earlier
    # "only if you are certain" made a matching Opus-high session hand a large job
    # to an Opus-high helper anyway, so the model is told to check first. A model
    # that cannot tell hands off, so a job never runs at the wrong level.
    keep = (
        "First check your own model and thinking level, which your instructions state. If you are "
        f"running on exactly {tier.label}, handle it yourself instead, because handing off would change "
        "nothing; if you cannot tell, hand it off. Also handle it yourself if the message only makes "
        "sense with this conversation's history."
    )
    if harness == "claude":
        return (
            f"{sized} Hand it to the `{tier.helper}` subagent, which runs on {tier.label}: give it the "
            "user's request plus any context from this conversation it needs, then give the user its "
            f"result, keeping its closing '{sign_off}' line. {keep}"
        )
    if harness == "codex":
        return (
            # Reason: a full-history fork inherits the parent's model, so the
            # override only applies with fork_turns "none"; the message carries
            # the context instead.
            f'{sized} Call spawn_agent with fork_turns "none", model "{tier.model_id}", reasoning_effort '
            f'"{tier.effort}", a short task_name, and a message holding the user\'s request plus any context '
            f"from this conversation it needs; tell it to end its reply with the line '{sign_off}'. Wait for "
            f"it, then give the user its result, keeping that line. {keep}"
        )
    # opencode moves this message onto the tier's model and thinking level itself,
    # so there is no hand-off to make: the model already answering only signs off.
    return f"{sized} This reply runs on {tier.label}. End it with the line '{sign_off}'."


def keep_note(verdict: Verdict) -> str:
    """Told when the router deliberately keeps a job. Without it, the session's own
    model copies the 'Done by' line from earlier helper replies and misattributes
    its answer (seen in opencode, where DeepSeek signed off as Claude Sonnet 5)."""
    why = (
        "it is a follow-up reply that needs this conversation"
        if verdict.follow_up
        else f"Jev is only {verdict.sure}% sure of its size"
    )
    # Reason: the note is about Jev's hand-off and the sign-off only; it must not
    # override what the user asked for (e.g. "yes, ask an agent to review it").
    return (
        f"Jev router: not routing this message ({why}). Carry on with it as you normally would. "
        "No Jev helper does this work, so add no 'Done by' line."
    )


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


def hook_json(context: str) -> str:
    """What Claude Code and Codex read from a UserPromptSubmit hook."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


def hook_output(harness: str, tier: Tier | None, verdict: Verdict) -> str:
    context = handoff(harness, tier, verdict) if tier else keep_note(verdict)
    if harness == "opencode":
        switch = {"model_id": tier.model_id, "variant": tier.effort, "agent": tier.helper} if tier else {}
        return json.dumps({**switch, "context": context})
    return hook_json(context)


INTERACTIVE_CODEX_SOURCES = {"cli", "vscode"}
SYSTEM_TURN_PREFIXES = ("<task-notification>",)


def interactive(harness: str, payload: dict) -> bool:
    """Whether a person is typing in this session. Headless runs (`claude -p`,
    `codex exec`, `opencode run`) are reviews and automation that pin their own
    model and thinking level, so the router never touches them, and never sends
    their text to Jev. Every check fails closed: an unknown signal means headless.
    """
    if harness == "claude":
        # Verified: "1" in the TUI, "0" under `claude -p`, set by Claude Code itself.
        return os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "1"
    if harness == "codex":
        # Two signals, both required. The transcript's first line records the front
        # end that started the session ("exec" for `codex exec`, "cli" or "vscode"
        # when a person types), but `codex exec resume` appends to an interactive
        # transcript without changing it; `codex exec` always runs with approvals
        # bypassed, which the hook sees as permission_mode (verified). A person who
        # also bypasses approvals is treated as headless: routing is lost, never
        # a review.
        if payload.get("permission_mode") == "bypassPermissions":
            return False
        with Path(payload["transcript_path"]).open() as transcript:
            meta = json.loads(transcript.readline())
        return meta["payload"]["source"] in INTERACTIVE_CODEX_SOURCES
    # opencode: hooks/opencode.js only calls the router from the TUI.
    return True


def route_cache_aware(config: dict, payload: dict, prompt: str) -> str:
    """Claude Code: price keeping the job against a fresh subagent. Only `live` mode
    tells the session anything; `shadow` and `capture` only log."""
    # Reason: taken here, before the transcript read and the Jev call, so the
    # snapshot's 6 s deadline is a budget shared with all of that work, not extra
    # time on top of it. It still leaves 2 s of the hook's 8 s timeout for process
    # startup and shutdown outside this function.
    started = time.monotonic()
    mode = config.get("mode", "shadow")
    event: dict = {
        "harness": "claude",
        "version": 3,
        "mode": mode,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        # Reason: lets the shadow report find the message in the transcript
        # without the log ever holding its text.
        "prompt_sha": usage.prompt_sha(prompt),
    }
    transcript = payload.get("transcript_path")
    current = sessions.read_session(Path(transcript)) if transcript else None
    if current is None:
        event["outcome"] = "no_session"
        record(event)
        return ""
    event.update(context=current.context, model=current.model, added=current.added, output=current.output)
    try:
        bins, table = calibration(), prices()
    except Exception as exc:  # a broken data file costs this message its opinion, and no Jev call
        event.update(outcome="error", error=error_label(exc))
        record(event)
        return ""
    parent = table.get(current.model)
    if parent is None:  # no price, no decision: Jev is not paid to size this message
        event["outcome"] = "unpriced"
        record(event)
        return ""
    tiers = TIERS["claude"]
    state = {"agent_previous_reply": current.previous_reply, "message": prompt[:MAX_MESSAGE_CHARS]}
    verdict = consult(config, event, prompt, tiers, parse_steps_verdict, state=state, questions=steps_questions(tiers))
    note = None
    if verdict is not None:
        tier = next(t for t in tiers if t.size == verdict.size)
        event.update(steps=verdict.steps, size=verdict.size, helper=tier.helper, helper_model=tier.model_id)
        sample = delegation.calls_for(bins, verdict.steps)
        decision = delegation.decide(
            sample,
            current.context,
            current.added,
            current.output,
            parent,
            # Reason: indexed, not checked: test_every_claude_tier_model_has_a_price
            # keeps every helper model in prices.json.
            table[tier.model_id],
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
    if note and mode == "capture":
        try:
            event["snapshot"] = snapshot.take_snapshot(
                fork_check_dir(config), payload, prompt, note, event, deadline=started + 6.0
            )
        except Exception as exc:  # a failed snapshot never touches the message
            event["snapshot_error"] = error_label(exc) if not isinstance(exc, snapshot.SnapshotError) else str(exc)
    record(event)
    return hook_json(note) if note and mode == "live" else ""


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
    # Reason: slash commands and skill invocations (including `/jev off`) are
    # instructions to the harness, not jobs to size. So is a background task's
    # completion notice, which Claude Code delivers through this same hook
    # (verified: a subagent's own prompt never fires it, but its notice does).
    if not prompt or prompt[0] in "/$" or prompt.startswith(SYSTEM_TURN_PREFIXES):
        return ""
    if harness == "claude":
        return route_cache_aware(config, payload, prompt)
    tiers = TIERS[harness]
    event: dict = {"harness": harness}
    verdict = consult(config, event, prompt, tiers, parse_verdict)
    if verdict is None:
        record(event)
        return ""
    tier = decide(verdict, tiers)
    event.update(outcome=outcome_of(verdict, tier), size=verdict.size, confidence=verdict.confidence)
    record(event)
    return hook_output(harness, tier, verdict)


# --- commands --------------------------------------------------------------------


def cmd_hook(harness: str) -> int:
    try:
        out = route(harness, sys.stdin.read())
    except BaseException:  # the router must never block or break a message
        out = ""
    if out:
        sys.stdout.write(out)
    return 0


def cmd_on(key_file: str | None) -> int:
    config = load_config()
    if key_file:
        config["key_file"] = str(Path(key_file).expanduser().resolve())
    if not read_key(config.get("key_file")):
        print(
            "Jev router stays OFF: it needs a shell file that sets OPENROUTER_API_KEY. "
            "Turn it on with: on --key-file <path>",
            file=sys.stderr,
        )
        return 1
    config["enabled"] = True
    save_config(config)
    print(f"Jev router is ON. {PRIVACY} Turn it off with: /jev off")
    return 0


def cmd_off() -> int:
    config = load_config()
    config["enabled"] = False
    save_config(config)
    print("Jev router is OFF. Messages no longer go to Jev.")
    return 0


def cmd_mode(mode: str, directory: str | None) -> int:
    config = load_config()
    config["mode"] = mode
    if directory:
        config["fork_check_dir"] = str(Path(directory).expanduser().resolve())
    save_config(config)
    print(
        {
            "shadow": "Jev router mode: shadow. In Claude Code it decides and logs, and tells the session nothing.",
            "capture": "Jev router mode: capture. Like shadow, and it saves a snapshot of each job it would "
            f"delegate, under {fork_check_dir(config)}.",
            "live": "Jev router mode: live. In Claude Code it tells the session to hand long jobs to a fresh subagent.",
        }[mode]
    )
    return 0


def status_text(config: dict, events: list[dict]) -> str:
    state = "ON" if config.get("enabled") else "OFF"
    lines = [f"Jev router is {state}." + (f" {PRIVACY}" if state == "ON" else " Turn it on with: /jev on")]
    messages = [e for e in events if e.get("harness") in HARNESSES]
    # Reason: a version-3 (Claude Code) event's size is not a 0.2.0 routing
    # decision, so it has its own line below and stays out of this table.
    sized = [e for e in messages if "size" in e and e.get("version") != 3]
    since = f" since {messages[0]['ts'][:10]}" if messages else ""
    lines.append(f"\nMessages Jev sized{since}: {len(sized)}")
    if sized:
        lines.append(f"  {'size':<10}{'messages':>9}{'routed':>8}")
        for size in JOBS:
            picked = [e for e in sized if e["size"] == size]
            routed = sum(e["outcome"] == "routed" for e in picked)
            lines.append(f"  {size:<10}{len(picked):>9}{routed:>8}")
        # Reason: in Claude Code and Codex the hook can only advise; the session
        # keeps a routed job when the helper would run on its own model.
        lines.append(
            "  Routed means the helper was offered the job; the session keeps it only if it is certain it "
            "already runs that model at that thinking level."
        )
    count = {k: sum(e.get("outcome") == k for e in messages) for k in ("follow_up", "unsure", "timeout", "error")}
    lines.append(
        f"Kept in the session: {count['follow_up']} follow-up replies, {count['unsure']} under "
        f"{MIN_CONFIDENCE}% sure. Carried on without Jev: {count['timeout']} timed out, {count['error']} errors."
    )
    mode = config.get("mode", "shadow")
    where = f" Snapshots go under {fork_check_dir(config)}." if mode == "capture" else ""
    lines.append(f"\nClaude Code mode: {mode}.{where}")
    cache_aware = [e for e in events if e.get("version") == 3]
    if cache_aware:
        delegated = sum(e.get("outcome") == "delegate" for e in cache_aware)
        snapshots = sum("snapshot" in e for e in cache_aware)
        lines.append(f"{len(cache_aware)} messages decided, {delegated} worth a fresh subagent, {snapshots} snapshots.")
    answered = [e for e in events if e.get("answered")]
    priced = [e["cost"] for e in answered if isinstance(e.get("cost"), int | float)]
    manual = sum(e.get("harness") == "classify" for e in answered)
    unpriced = len(answered) - len(priced)
    abandoned = sum(e.get("outcome") == "timeout" for e in events)
    lines.append(
        f"\nJev has cost ${sum(priced):.4f} over {len(answered)} answered calls"
        + (f" ({manual} of them from classify)" if manual else "")
        + (f", {unpriced} of which reported no cost" if unpriced else "")
        + (f", plus {abandoned} timed-out calls whose cost was never reported" if abandoned else "")
        + "."
    )
    return "\n".join(lines)


def cmd_status() -> int:
    print(status_text(load_config(), read_log()))
    return 0


def cmd_classify(harness: str, messages: list[str]) -> int:
    """Size messages by hand and print a markdown table. Works while the router is off."""
    config = load_config()
    key = read_key(config.get("key_file"))
    if not key:
        print("No OpenRouter key: run `on --key-file <path>` once, then `off` if you want it off.", file=sys.stderr)
        return 1
    tiers = TIERS[harness]
    timeout = timeout_of(config)
    print("| # | message | Jev picked | sure | follow-up | router does | Jev took | cost |")
    print("|---|---|---|---|---|---|---|---|")
    for n, message in enumerate(messages, 1):
        started = time.monotonic()
        event: dict = {"harness": "classify"}
        body = None
        try:
            body = ask_jev(message, tiers, key, timeout)
            verdict = parse_verdict(body, tiers)
        except Exception as exc:
            took = time.monotonic() - started
            event.update(outcome="timeout" if isinstance(exc, TimeoutError) else "error")
            if body is not None:
                event.update(answered=True, cost=cost_of(body))
            record(event)
            print(f"| {n} | {message} | {error_label(exc)} | | | carries on without Jev | {took:.1f}s | |")
            continue
        took = time.monotonic() - started
        tier = decide(verdict, tiers)
        cost = cost_of(body)
        event.update(answered=True, size=verdict.size, confidence=verdict.confidence, cost=cost)
        record(event)
        does = f"hands to {tier.label}" if tier else "keeps it in the session"
        print(
            f"| {n} | {message} | {verdict.size} | {verdict.sure}% | "
            f"{'yes' if verdict.follow_up else 'no'} | {does} | {took:.1f}s | "
            f"{f'${cost:.5f}' if cost is not None else '?'} |"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Size every message with Jev and send small jobs to a smaller model.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="run as a per-message hook").add_argument("harness", choices=HARNESSES)
    on = sub.add_parser("on", help="turn the router on")
    on.add_argument("--key-file", help="shell file that sets OPENROUTER_API_KEY (remembered)")
    sub.add_parser("off", help="turn the router off")
    sub.add_parser("status", help="show the switch, the counts per size, and what Jev has cost")
    classify = sub.add_parser("classify", help="size messages by hand and print a table")
    classify.add_argument("--harness", choices=HARNESSES, default="claude")
    classify.add_argument("messages", nargs="+")
    mode = sub.add_parser("mode", help="shadow, capture or live (Claude Code)")
    mode.add_argument("mode", choices=MODES)
    mode.add_argument("--dir", help="where capture saves snapshots")
    args = parser.parse_args(argv)

    if args.command == "hook":
        return cmd_hook(args.harness)
    if args.command == "on":
        return cmd_on(args.key_file)
    if args.command == "off":
        return cmd_off()
    if args.command == "status":
        return cmd_status()
    if args.command == "mode":
        return cmd_mode(args.mode, args.dir)
    return cmd_classify(args.harness, args.messages)


if __name__ == "__main__":
    raise SystemExit(main())
