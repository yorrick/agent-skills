#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Jev router: size every message with Jev and send small jobs to a smaller model.

One script serves Claude Code, Codex and opencode. Each harness's per-message
hook runs `hook <harness>` with the user's message as JSON on stdin. The user
flips the router with `on` and `off`, reads the counters with `status`, and can
size messages by hand with `classify`.

The router must never get in the way of a message. When it is off, when Jev is
slow, or when anything at all fails, `hook` prints nothing and exits 0, which
every harness treats as "no opinion": the message goes through unchanged.

Standard library only, on purpose: the hook runs before every message, and a
dependency would add resolver time to each one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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

PRIVACY = (
    "While it is on, the text of every message you send also goes to OpenRouter and to TypeSafe "
    "(the company that makes Jev). Keep it off for private work."
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
    confidence: float  # the probability Jev gives its pick, in percent
    follow_up: bool


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
    the log, and the log never keeps anything from a response but its cost.
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
    return Verdict(
        size=size,
        # Reason: rounded so 0.596 reads 59.6, not 59.599999999999994; never
        # rounded to a whole number, so 59.6% is still under 60%.
        confidence=round(probability(picked) * 100, 2),
        follow_up=probability(follow_up) >= FOLLOW_UP_AT,
    )


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


def ask_jev(message: str, tiers: tuple[Tier, ...], key: str, timeout: float) -> dict:
    """One call to Jev, abandoned at `timeout` seconds of wall-clock time.

    urlopen's own timeout bounds each socket operation, not the whole exchange, so a
    slow trickle could outlast it; the daemon thread gives a hard overall deadline.
    """
    body = json.dumps(
        {
            "model": JEV_MODEL,
            "state": {"message": message[:MAX_MESSAGE_CHARS]},
            "questions": jev_questions(tiers),
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


def decide(verdict: Verdict, tiers: tuple[Tier, ...]) -> Tier | None:
    """The tier to hand the job to, or None when the session should keep it."""
    if verdict.follow_up or verdict.confidence < MIN_CONFIDENCE:
        return None
    return next(t for t in tiers if t.size == verdict.size)


def outcome_of(verdict: Verdict, tier: Tier | None) -> str:
    if tier is not None:
        return "routed"
    return "follow_up" if verdict.follow_up else "unsure"


# --- what each harness is told -------------------------------------------------


def handoff(harness: str, tier: Tier, verdict: Verdict) -> str:
    sized = f"Jev router: Jev's size for this message is {tier.size} ({verdict.confidence:g}% sure)."
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
        else f"Jev is only {verdict.confidence:g}% sure of its size"
    )
    # Reason: the note is about Jev's hand-off and the sign-off only; it must not
    # override what the user asked for (e.g. "yes, ask an agent to review it").
    return (
        f"Jev router: not routing this message ({why}). Carry on with it as you normally would. "
        "No Jev helper does this work, so add no 'Done by' line."
    )


def hook_output(harness: str, tier: Tier | None, verdict: Verdict) -> str:
    context = handoff(harness, tier, verdict) if tier else keep_note(verdict)
    if harness == "opencode":
        switch = {"model_id": tier.model_id, "variant": tier.effort, "agent": tier.helper} if tier else {}
        return json.dumps({**switch, "context": context})
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


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


def route(harness: str, stdin: str) -> str:
    """What the hook prints: a hand-off, or '' for "carry on as if I wasn't here"."""
    config = load_config()
    if not config.get("enabled"):
        return ""
    payload = json.loads(stdin)
    if not interactive(harness, payload):
        return ""
    prompt = str(payload.get("prompt") or "").strip()
    # Reason: slash commands and skill invocations (including `/jev off`) are
    # instructions to the harness, not jobs to size. So is a background task's
    # completion notice, which Claude Code delivers through this same hook
    # (verified: a subagent's own prompt never fires it, but its notice does).
    if not prompt or prompt[0] in "/$" or prompt.startswith(SYSTEM_TURN_PREFIXES):
        return ""
    tiers = TIERS[harness]
    event: dict = {"harness": harness}
    started = time.monotonic()
    body = verdict = tier = None
    try:
        key = read_key(config.get("key_file"))
        if not key:
            raise NoKey("no OpenRouter API key in the key file")
        body = ask_jev(prompt, tiers, key, timeout_of(config))
        verdict = parse_verdict(body, tiers)
    except TimeoutError:
        event["outcome"] = "timeout"
    except Exception as exc:  # any failure means "no opinion"
        event.update(outcome="error", error=error_label(exc))
    event["latency_ms"] = round((time.monotonic() - started) * 1000)
    if body is not None:
        # Reason: nothing from the response body is logged except its cost; any
        # string field could carry echoed message text.
        event.update(answered=True, cost=cost_of(body))
    if verdict is not None:
        tier = decide(verdict, tiers)
        event.update(outcome=outcome_of(verdict, tier), size=verdict.size, confidence=verdict.confidence)
    record(event)
    return hook_output(harness, tier, verdict) if verdict else ""


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


def status_text(config: dict, events: list[dict]) -> str:
    state = "ON" if config.get("enabled") else "OFF"
    lines = [f"Jev router is {state}." + (f" {PRIVACY}" if state == "ON" else " Turn it on with: /jev on")]
    messages = [e for e in events if e.get("harness") in HARNESSES]
    sized = [e for e in messages if "size" in e]
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
            f"| {n} | {message} | {verdict.size} | {verdict.confidence:g}% | "
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
    args = parser.parse_args(argv)

    if args.command == "hook":
        return cmd_hook(args.harness)
    if args.command == "on":
        return cmd_on(args.key_file)
    if args.command == "off":
        return cmd_off()
    if args.command == "status":
        return cmd_status()
    return cmd_classify(args.harness, args.messages)


if __name__ == "__main__":
    raise SystemExit(main())
