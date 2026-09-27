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

API_URL = os.environ.get("JEV_ROUTER_API_URL", "https://openrouter.ai/api/v1/chat/completions")
JEV_MODEL = "typesafe/jev-router"
MIN_CONFIDENCE = 60
DEFAULT_TIMEOUT_SECONDS = 6.0
# Reason: Jev only needs the gist to size a job; a long paste would cost more and
# answer slower without changing the verdict.
MAX_MESSAGE_CHARS = 4000
HARNESSES = ("claude", "codex", "opencode")


@dataclass(frozen=True)
class Tier:
    """One size Jev can pick, and the model that takes jobs of that size."""

    size: str
    jobs: str
    model: str
    model_id: str
    helper: str


def load_tiers() -> tuple[dict[str, str], dict[str, tuple[Tier, ...]]]:
    """Read tiers.json, which the opencode hook reads too.

    Each harness gets one size per model it actually has, smallest first. Codex has
    three current models for four sizes, so its smallest also takes the everyday
    jobs (`also`): tiny and everyday are both single-shot writing, while the step
    from everyday to large (a multi-step build) is where capability starts to
    matter. A Codex plugin cannot ship agent definitions, but spawn_agent takes a
    model directly, so a Codex helper is defined at the moment it is spawned.
    """
    data = json.loads((Path(__file__).with_name("tiers.json")).read_text())
    jobs: dict[str, str] = data["jobs"]
    tiers = {
        harness: tuple(
            Tier(
                size=t["size"],
                jobs="; or ".join(jobs[s] for s in (*t.get("also", ()), t["size"])),
                model=t["model"],
                model_id=t["model_id"],
                helper=t["helper"],
            )
            for t in entries
        )
        for harness, entries in data["harnesses"].items()
    }
    return jobs, tiers


JOBS, TIERS = load_tiers()

PRIVACY = (
    "While it is on, the text of every message you send also goes to OpenRouter, to TypeSafe "
    "(the company that makes Jev), and to whichever model Jev picks to answer its sizing "
    "question. Keep it off for private work."
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
    confidence: int
    follow_up: bool
    cost: float | None
    answered_by: str | None


def jev_instructions(tiers: tuple[Tier, ...]) -> str:
    sizes = "\n".join(f"- {t.size}: {t.jobs}" for t in tiers)
    choices = " | ".join(f'"{t.size}"' for t in tiers)
    return (
        "You size jobs for a model router. Answer one question: what is the SMALLEST model size "
        "that can do this job well?\n\n"
        f"Sizes, smallest first:\n{sizes}\n\n"
        "Also set follow_up to true when the message is a short reply that only makes sense inside "
        'an ongoing conversation (for example "yes do that but make it shorter"), because you cannot '
        "see that conversation.\n\n"
        "Reply with one JSON object and nothing else:\n"
        f'{{"size": {choices}, "confidence": <integer 0-100, how sure you are of the size>, '
        '"follow_up": true | false}'
    )


def parse_verdict(body: dict, tiers: tuple[Tier, ...]) -> Verdict:
    content = body["choices"][0]["message"]["content"]
    found = re.search(r"\{.*\}", content, re.S)
    if not found:
        raise ValueError(f"Jev did not answer with JSON: {content[:80]!r}")
    answer = json.loads(found.group(0))
    size = answer.get("size")
    if size not in {t.size for t in tiers}:
        raise ValueError(f"Jev picked an unknown size: {size!r}")
    confidence = round(float(answer["confidence"]))
    if not 0 <= confidence <= 100:
        raise ValueError(f"Jev's confidence is out of range: {confidence}")
    usage = body.get("usage") or {}
    return Verdict(
        size=size,
        confidence=confidence,
        follow_up=answer.get("follow_up") is True,
        cost=usage.get("cost"),
        answered_by=body.get("model"),
    )


def ask_jev(message: str, tiers: tuple[Tier, ...], key: str, timeout: float) -> Verdict:
    """One call to Jev, abandoned at `timeout` seconds of wall-clock time.

    urlopen's own timeout bounds each socket operation, not the whole exchange, so a
    slow trickle could outlast it; the daemon thread gives a hard overall deadline.
    """
    body = json.dumps(
        {
            "model": JEV_MODEL,
            "messages": [
                {"role": "system", "content": jev_instructions(tiers)},
                {"role": "user", "content": message[:MAX_MESSAGE_CHARS]},
            ],
            "usage": {"include": True},
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
    return parse_verdict(outcome["body"], tiers)


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
    sized = f"Jev router: Jev sized this message as a {tier.size} job ({verdict.confidence}% sure)."
    sign_off = f"Done by {tier.model}"
    keep = (
        f"Handle it yourself instead if you are already running on {tier.model}, or if the message "
        "only makes sense with this conversation's history."
    )
    if harness == "claude":
        return (
            f"{sized} Hand it to the `{tier.helper}` subagent, which runs on {tier.model}: give it the "
            "user's request plus any context from this conversation it needs, then give the user its "
            f"result, keeping its closing '{sign_off}' line. {keep}"
        )
    if harness == "codex":
        return (
            f'{sized} Call spawn_agent with model "{tier.model_id}", a short task_name, and a message '
            "holding the user's request plus any context from this conversation it needs, and tell it "
            f"to end its reply with the line '{sign_off}'. Wait for it, then give the user its result, "
            f"keeping that line. {keep}"
        )
    # opencode moves this message onto the tier's model itself, so there is no
    # hand-off to make: the model that is already answering only signs off.
    return f"{sized} This reply runs on {tier.model}. End it with the line '{sign_off}'."


def hook_output(harness: str, tier: Tier, verdict: Verdict) -> str:
    context = handoff(harness, tier, verdict)
    if harness == "opencode":
        return json.dumps({"size": tier.size, "model_id": tier.model_id, "agent": tier.helper, "context": context})
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


def route(harness: str, stdin: str) -> str:
    """What the hook prints: a hand-off, or '' for "carry on as if I wasn't here"."""
    config = load_config()
    if not config.get("enabled"):
        return ""
    prompt = str(json.loads(stdin).get("prompt") or "").strip()
    # Reason: slash commands and skill invocations (including `/jev off`) are
    # instructions to the harness, not jobs to size.
    if not prompt or prompt[0] in "/$":
        return ""
    tiers = TIERS[harness]
    event: dict = {"harness": harness}
    started = time.monotonic()
    verdict = tier = None
    try:
        key = read_key(config.get("key_file"))
        if not key:
            raise RuntimeError("no OpenRouter API key in the key file")
        verdict = ask_jev(prompt, tiers, key, float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)))
    except TimeoutError:
        event["outcome"] = "timeout"
    except Exception as exc:  # any failure means "no opinion"
        event.update(outcome="error", error=f"{type(exc).__name__}: {exc}"[:200])
    event["latency_ms"] = round((time.monotonic() - started) * 1000)
    if verdict is not None:
        tier = decide(verdict, tiers)
        event.update(
            outcome=outcome_of(verdict, tier),
            size=verdict.size,
            confidence=verdict.confidence,
            cost=verdict.cost,
            answered_by=verdict.answered_by,
        )
    record(event)
    return hook_output(harness, tier, verdict) if tier and verdict else ""


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
        lines.append(f"  {'size':<10}{'messages':>9}{'sent to helper':>16}")
        for size in JOBS:
            picked = [e for e in sized if e["size"] == size]
            routed = sum(e["outcome"] == "routed" for e in picked)
            lines.append(f"  {size:<10}{len(picked):>9}{routed:>16}")
    count = {k: sum(e.get("outcome") == k for e in messages) for k in ("follow_up", "unsure", "timeout", "error")}
    lines.append(
        f"Kept in the session: {count['follow_up']} follow-up replies, {count['unsure']} under "
        f"{MIN_CONFIDENCE}% sure. Carried on without Jev: {count['timeout']} timed out, {count['error']} errors."
    )
    answered = [e for e in events if isinstance(e.get("cost"), int | float)]
    manual = sum(e.get("harness") == "classify" for e in answered)
    abandoned = sum(e.get("outcome") == "timeout" for e in events)
    lines.append(
        f"\nJev has cost ${sum(e['cost'] for e in answered):.4f} over {len(answered)} answered calls"
        + (f" ({manual} of them from classify)" if manual else "")
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
    timeout = float(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    print("| # | message | Jev picked | sure | follow-up | router does | Jev took | cost |")
    print("|---|---|---|---|---|---|---|---|")
    for n, message in enumerate(messages, 1):
        started = time.monotonic()
        event: dict = {"harness": "classify"}
        try:
            verdict = ask_jev(message, tiers, key, timeout)
        except Exception as exc:
            took = time.monotonic() - started
            event.update(outcome="timeout" if isinstance(exc, TimeoutError) else "error")
            record(event)
            print(f"| {n} | {message} | {type(exc).__name__} | | | carries on without Jev | {took:.1f}s | |")
            continue
        took = time.monotonic() - started
        tier = decide(verdict, tiers)
        event.update(size=verdict.size, confidence=verdict.confidence, cost=verdict.cost)
        record(event)
        does = f"hands to {tier.model}" if tier else "keeps it in the session"
        cost = f"${verdict.cost:.5f}" if verdict.cost is not None else "?"
        print(
            f"| {n} | {message} | {verdict.size} | {verdict.confidence}% | "
            f"{'yes' if verdict.follow_up else 'no'} | {does} | {took:.1f}s | {cost} |"
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
