"""Behaviour tests for the jev-router plugin's core script.

The hook runs as a real subprocess against a local stand-in for OpenRouter, so
the tests cover exactly what a harness sees: stdout and the exit code. The one
promise that matters most is that the router never gets in the way, so every
failure mode is checked to print nothing and exit 0.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "jev-router"
SCRIPT = PLUGIN / "skills" / "jev" / "scripts" / "jev_router.py"
TIERS_FILE = SCRIPT.with_name("tiers.json")

sys.path.insert(0, str(SCRIPT.parent))
_spec = importlib.util.spec_from_file_location("jev_router", SCRIPT)
assert _spec and _spec.loader
jev_router = importlib.util.module_from_spec(_spec)
# Reason: dataclasses look their module up in sys.modules while it executes.
sys.modules["jev_router"] = jev_router
_spec.loader.exec_module(jev_router)

from test_jev_usage import assistant, typed  # noqa: E402


def jev_answers(size: str, confidence: float, follow_up: bool) -> dict:
    """What Jev's Decisions API returns for the router's two questions: the size
    it picks with `confidence` percent probability, the rest spread evenly."""
    others = [s for s in ("tiny", "everyday", "large", "hardest") if s != size]
    rest = round((1 - confidence / 100) / len(others), 4)
    return {
        "size": {
            "type": "choice",
            "choice": size,
            "confidence": 0.5,
            "probabilities": {**dict.fromkeys(others, rest), size: confidence / 100},
        },
        "follow_up": {"type": "noul", "noul": 0.9 if follow_up else 0.1},
    }


class FakeJev:
    """A local OpenRouter stand-in that answers with whatever the test sets:
    `answer` in the router's terms, or `raw_answers` sent verbatim."""

    def __init__(self) -> None:
        self.answer: dict = {"size": "tiny", "confidence": 97, "follow_up": False}
        self.raw_answers: object = None
        self.status = 200
        self.delay = 0.0
        self.usage: dict = {"input_tokens": 480, "output_tokens": 60, "cost": 0.00002}
        self.model = "typesafe/jev-1.13-20260917"
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                time.sleep(fake.delay)
                answers = fake.raw_answers if fake.raw_answers is not None else jev_answers(**fake.answer)
                body = json.dumps(
                    {
                        "id": "gen-dec-1",
                        "model": fake.model,
                        "provider": "TypeSafe",
                        "answers": answers,
                        "usage": fake.usage,
                    }
                ).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/alpha/decisions"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def jev() -> Iterator[FakeJev]:
    fake = FakeJev()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    key_file = tmp_path / "openrouter.sh"
    key_file.write_text('export OPENROUTER_API_KEY="sk-or-test"\n')
    return tmp_path / "state"


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


def switch_on(home: Path, jev: FakeJev, **extra: object) -> None:
    assert run(home, jev, "on", "--key-file", str(home.parent / "openrouter.sh")).returncode == 0
    if extra:
        config = json.loads((home / "config.json").read_text())
        (home / "config.json").write_text(json.dumps({**config, **extra}))


def hook(
    home: Path,
    jev: FakeJev,
    harness: str = "codex",
    prompt: str = "rename foo to bar",
    attended: str | None = "1",
    source: str | None = "cli",
    permission_mode: str = "default",
    env: dict | None = None,
) -> str:
    """One message through the hook. `source` is what the Codex transcript's first
    line records: "cli" or "vscode" when a person types, "exec" under `codex exec`;
    None means the transcript does not exist."""
    transcript = home.parent / "codex-transcript.jsonl"
    if source is not None:
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"source": source}}) + "\n")
    payload = {
        "prompt": prompt,
        "session_id": "s",
        "transcript_path": str(transcript),
        "permission_mode": permission_mode,
    }
    result = run(home, jev, "hook", harness, stdin=json.dumps(payload), attended=attended, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def log(home: Path) -> list[dict]:
    path = home / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# --- the switch --------------------------------------------------------------------


def test_off_until_switched_on(home: Path, jev: FakeJev) -> None:
    assert hook(home, jev) == ""
    assert jev.requests == []
    assert log(home) == []


def test_on_needs_a_key_file_and_stays_off_without_one(home: Path, jev: FakeJev) -> None:
    result = run(home, jev, "on")
    assert result.returncode == 1
    assert "stays OFF" in result.stderr
    assert hook(home, jev) == ""


def test_on_warns_about_openrouter_and_off_stops_calls(home: Path, jev: FakeJev) -> None:
    result = run(home, jev, "on", "--key-file", str(home.parent / "openrouter.sh"))
    assert "ON" in result.stdout and "OpenRouter" in result.stdout and "TypeSafe" in result.stdout
    assert run(home, jev, "off").returncode == 0
    assert hook(home, jev) == ""
    assert jev.requests == []


def test_key_file_formats() -> None:
    assert jev_router.KEY_LINE.match('export OPENROUTER_API_KEY="abc"').group(1) == "abc"
    assert jev_router.KEY_LINE.match("OPENROUTER_API_KEY=abc").group(1) == "abc"
    assert jev_router.KEY_LINE.match("set -gx OPENROUTER_API_KEY abc").group(1) == "abc"
    assert jev_router.KEY_LINE.match("export OTHER_KEY=abc") is None


# --- routing -------------------------------------------------------------------------


def test_sixty_percent_is_sure_enough_and_below_is_kept(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "large", "confidence": 60, "follow_up": False}
    routed = json.loads(hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert 'spawn_agent with fork_turns "none", model "gpt-6-sol", reasoning_effort "high"' in routed
    jev.answer = {"size": "large", "confidence": 59, "follow_up": False}
    context = json.loads(hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert "not routing this message" in context and "59% sure" in context
    assert "Carry on with it as you normally would" in context
    assert "spawn_agent" not in context
    assert [e["outcome"] for e in log(home)] == ["routed", "unsure"]


def test_follow_up_replies_stay_in_the_session_without_a_sign_off(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "tiny", "confidence": 99, "follow_up": True}
    context = json.loads(hook(home, jev, prompt="yes do that but make it shorter"))["hookSpecificOutput"]
    assert "follow-up" in context["additionalContext"]
    assert "add no 'Done by' line" in context["additionalContext"]
    assert log(home)[-1]["outcome"] == "follow_up"


def test_commands_skill_calls_and_task_notices_are_not_sent_to_jev(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    notice = "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n</task-notification>"
    for prompt in ("/jev off", "$jev status", "   ", notice):
        assert hook(home, jev, prompt=prompt) == ""
    assert jev.requests == []


def test_jev_itself_gets_two_typed_questions_and_a_truncated_message(home: Path, jev: FakeJev) -> None:
    """Jev itself, not `typesafe/jev-router`, OpenRouter's chat router that hands
    the prompt to another model and returns that model's text."""
    switch_on(home, jev)
    hook(home, jev, prompt="x" * 10_000)
    [request] = jev.requests
    assert request["model"] == "typesafe/jev-1.13"
    assert set(request) == {"model", "state", "questions", "provider"}
    # The message may reach TypeSafe and no other provider, with no fallback.
    assert request["provider"] == {"only": ["typesafe"], "allow_fallbacks": False}
    size, follow_up = request["questions"]["size"], request["questions"]["follow_up"]
    assert size["type"] == "choice"
    assert list(size["criteria"]) == ["tiny", "everyday", "large", "hardest"]
    assert size["criteria"]["tiny"] == jev_router.JOBS["tiny"]
    assert follow_up["type"] == "noul" and set(follow_up["criteria"]) == {"true", "false"}
    assert len(request["state"]["message"]) == jev_router.MAX_MESSAGE_CHARS


def test_codex_spawns_with_the_model_and_thinking_level(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "everyday", "confidence": 90, "follow_up": False}
    context = json.loads(hook(home, jev, "codex"))["hookSpecificOutput"]["additionalContext"]
    # A full-history fork inherits the parent's model, so the override needs fork_turns "none".
    assert 'spawn_agent with fork_turns "none", model "gpt-6-luna", reasoning_effort "max"' in context
    assert "Done by GPT-6 Luna at max thinking" in context
    assert "If you are running on exactly GPT-6 Luna at max thinking, handle it yourself" in context


def test_opencode_gets_a_switch_it_can_apply(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "hardest", "confidence": 90, "follow_up": False}
    out = json.loads(hook(home, jev, "opencode"))
    assert (out["agent"], out["model_id"], out["variant"]) == ("jev-hardest", "openrouter/z-ai/glm-5.3-flash", "max")
    assert "Done by GLM 5.3 flash at max thinking" in out["context"]


# --- interactive sessions only ---------------------------------------------------------


@pytest.mark.parametrize("attended", ["0", None])
def test_headless_claude_is_never_routed_or_sent_to_jev(home: Path, jev: FakeJev, attended: str | None) -> None:
    """`claude -p` sets CLAUDE_CODE_SESSION_ATTENDED=0: a review pins its own model."""
    switch_on(home, jev)
    assert hook(home, jev, harness="claude", attended=attended) == ""
    assert jev.requests == [] and log(home) == []


@pytest.mark.parametrize("source", ["exec", "mcp", None])
def test_headless_codex_is_never_routed_or_sent_to_jev(home: Path, jev: FakeJev, source: str | None) -> None:
    switch_on(home, jev)
    (home.parent / "codex-transcript.jsonl").unlink(missing_ok=True)
    assert hook(home, jev, "codex", source=source) == ""
    assert jev.requests == []


def test_codex_exec_resuming_an_interactive_session_is_headless(home: Path, jev: FakeJev) -> None:
    """`codex exec resume` keeps the interactive transcript's "cli" header, but every
    `codex exec` runs with approvals bypassed, which the hook sees."""
    switch_on(home, jev)
    assert hook(home, jev, "codex", source="cli", permission_mode="bypassPermissions") == ""
    assert jev.requests == []


def test_codex_from_the_ide_is_interactive(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    assert "spawn_agent" in hook(home, jev, "codex", source="vscode")


# --- JEV_ROUTER on/off and the replay note --------------------------------------------


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


def test_note_file_is_never_printed_for_a_notice_or_a_command(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    """Final Important 2: in the delegate replay, the helper's completion notice
    comes through the same hook; the note again would hand the job off twice."""
    note = tmp_path / "note.txt"
    note.write_text("Jev router: hand this job to the `jev-router:large` subagent.")
    env = {"JEV_ROUTER": "off", "JEV_ROUTER_NOTE_FILE": str(note)}
    notice = "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n</task-notification>"
    for prompt in (notice, "/jev status", "$jev status", "   "):
        assert hook(home, jev, harness="claude", prompt=prompt, attended="0", env=env) == ""
    assert jev.requests == [] and log(home) == []


# --- never in the way ----------------------------------------------------------------


def test_slow_jev_is_abandoned_at_the_deadline(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, timeout_seconds=0.5)
    jev.delay = 5
    started = time.monotonic()
    assert hook(home, jev) == ""
    assert time.monotonic() - started < 3
    assert log(home)[-1]["outcome"] == "timeout"


def _answers(size: dict | None = None, follow_up: dict | None = None) -> dict:
    good = jev_answers("tiny", 90, False)
    return {"size": {**good["size"], **(size or {})}, "follow_up": {**good["follow_up"], **(follow_up or {})}}


@pytest.mark.parametrize(
    "answers",
    [
        "I think this is small.",
        {},
        {"size": _answers()["size"]},
        _answers(size={"choice": "medium", "probabilities": {"medium": 0.9}}),
        _answers(size={"probabilities": {"tiny": 2.5}}),
        _answers(size={"probabilities": {"tiny": "high"}}),
        _answers(size={"probabilities": {"tiny": True}}),
        _answers(size={"probabilities": {"everyday": 0.9}}),
        _answers(size={"type": "score"}),
        _answers(follow_up={"noul": "no"}),
        _answers(follow_up={"noul": -0.1}),
        _answers(follow_up={"type": "choice"}),
    ],
)
def test_unusable_answers_are_ignored_but_still_cost_money(home: Path, jev: FakeJev, answers: object) -> None:
    switch_on(home, jev)
    jev.raw_answers = answers
    assert hook(home, jev) == ""
    event = log(home)[-1]
    assert event["outcome"] == "error" and event["cost"] == 0.00002


@pytest.mark.parametrize("body", [{"usage": [{"cost": 0.02}], "model": ["x"]}, {"usage": "free"}, [], "text"])
def test_odd_response_shapes_never_break_the_hook(body: object) -> None:
    assert jev_router.cost_of(body) is None
    tiers = jev_router.TIERS["claude"]
    with pytest.raises(jev_router.BadAnswer):
        jev_router.parse_verdict(body, tiers)


def test_the_timeout_can_be_lowered_but_not_raised_past_the_outer_cap() -> None:
    assert jev_router.timeout_of({}) == 2
    assert jev_router.timeout_of({"timeout_seconds": 0.5}) == 0.5
    assert jev_router.timeout_of({"timeout_seconds": 30}) == 2


@pytest.mark.parametrize("routed", [True, False])
def test_the_log_never_keeps_what_jev_said(home: Path, jev: FakeJev, routed: bool) -> None:
    """No string field of the response is logged, whether or not it is usable."""
    switch_on(home, jev)
    jev.model = "customer/SECRET-PROJECT-X"
    if not routed:
        jev.raw_answers = {"size": {"type": "choice", "choice": "SECRET-PROJECT-X"}}
    hook(home, jev, prompt="rename SECRET-PROJECT-X")
    assert "SECRET" not in (home / "log.jsonl").read_text()


@pytest.mark.parametrize("usable", [True, False])
def test_the_claude_code_log_never_keeps_the_reply_or_what_jev_said(home: Path, jev: FakeJev, usable: bool) -> None:
    """The version-3 path: neither the previous reply ("Ready.") nor any string
    Jev returned reaches the event, whether or not the answer is usable."""
    switch_on(home, jev)
    jev.model = "customer/SECRET-PROJECT-X"
    answers = steps_answers(3.5)
    if not usable:
        answers["size"]["choice"] = "SECRET-PROJECT-X"
    jev.raw_answers = answers
    claude_hook(home, jev, prompt="rename SECRET-PROJECT-X in the export")
    (event,) = log(home)
    assert event["version"] == 3
    assert event["outcome"] == ("delegate" if usable else "error")
    text = (home / "log.jsonl").read_text()
    assert "SECRET" not in text and "Ready." not in text and "export" not in text


def test_status_counts_an_answered_call_that_reported_no_cost(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.usage = {}
    routed = json.loads(hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert 'spawn_agent with fork_turns "none", model "gpt-6-luna", reasoning_effort "medium"' in routed
    text = run(home, jev, "status").stdout
    assert "$0.0000 over 1 answered calls, 1 of which reported no cost." in text


@pytest.mark.parametrize(("confidence", "shown"), [(59.6, "59.6"), (59.999, "59.999"), (59.99999, "just under 60")])
def test_confidence_just_under_sixty_is_not_rounded_up(home: Path, jev: FakeJev, confidence: float, shown: str) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "tiny", "confidence": confidence, "follow_up": False}
    assert f"Jev is only {shown}% sure" in hook(home, jev)
    [event] = log(home)
    assert event["outcome"] == "unsure"
    assert event["confidence"] == pytest.approx(confidence) and event["confidence"] < 60


def test_sixty_percent_exactly_is_shown_as_sixty() -> None:
    verdict = jev_router.Verdict(size="tiny", probability=0.6, follow_up=False)
    assert verdict.sure == "60"
    assert jev_router.decide(verdict, jev_router.TIERS["claude"]) is not None


def test_http_errors_are_ignored(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.status = 500
    assert hook(home, jev) == ""
    assert log(home)[-1]["outcome"] == "error"


def test_a_missing_key_file_is_ignored(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    (home.parent / "openrouter.sh").unlink()
    assert hook(home, jev) == ""
    assert "no OpenRouter API key" in log(home)[-1]["error"]


def test_garbage_on_stdin_is_ignored(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    result = run(home, jev, "hook", "claude", stdin="not json")
    assert (result.returncode, result.stdout) == (0, "")


# --- Claude Code: keep the job, or brief a fresh subagent ----------------------------

CALIBRATION = {
    "bins": [{"max_score": 1.0, "calls": [1, 1, 2, 2, 3]}, {"max_score": None, "calls": [12, 20, 30, 40, 60]}]
}


def steps_answers(score: float, size: str = "large", p: float = 0.8) -> dict:
    others = [s for s in ("tiny", "everyday", "large", "hardest") if s != size]
    return {
        "steps": {"type": "score", "score": score, "probabilities": dict.fromkeys("01234", 0.2)},
        "size": {
            "type": "choice",
            "choice": size,
            "confidence": 0.5,
            "probabilities": {**dict.fromkeys(others, round((1 - p) / 3, 4)), size: p},
        },
    }


def claude_hook(
    home: Path,
    jev: FakeJev,
    *,
    prompt: str = "build the whole export feature",
    entries: list[dict] | None = None,
    env: dict | None = None,
) -> str:
    calibration = home.parent / "calibration.json"
    calibration.write_text(json.dumps(CALIBRATION))
    transcript = home.parent / "claude-transcript.jsonl"
    if entries is None:
        entries = [typed("start"), assistant("r1", read=800_000, w1h=3_000, out=1_500, text="Ready.")]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    payload = {
        "prompt": prompt,
        "session_id": "sess-1",
        "transcript_path": str(transcript),
        "cwd": str(home.parent),
        "permission_mode": "default",
    }
    result = run(
        home,
        jev,
        "hook",
        "claude",
        stdin=json.dumps(payload),
        env={"JEV_ROUTER_CALIBRATION": str(calibration), **(env or {})},
    )
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
    (event,) = log(home)
    assert event["outcome"] == "unpriced" and event["model"] == "claude-future-9"
    assert jev.requests == []  # nothing was paid for


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
    assert log(home)[0]["outcome"] == "error"
    assert jev.requests == []  # nothing was paid for
    # Reason: the steps answers have no follow_up, so Codex would reject them for
    # the wrong reason; its default answer shows the broken file does not reach it.
    jev.raw_answers = None
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


def test_the_fork_check_folder_is_absolute_under_a_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling F43: with a relative JEV_ROUTER_HOME and no `--dir`, the default
    folder is still an absolute, resolved path, so every clone path under it
    (and every path a replay's venv records) is absolute too. A relative
    `fork_check_dir` in the config is resolved the same way."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JEV_ROUTER_HOME", "relative-home")
    assert jev_router.fork_check_dir({}) == (tmp_path / "relative-home" / "fork-check").resolve()
    assert jev_router.fork_check_dir({"fork_check_dir": "relative-fork"}) == (tmp_path / "relative-fork").resolve()
    assert jev_router.fork_check_dir({}).is_absolute()


@pytest.mark.parametrize("name", ["fork check", "fork-chéck"], ids=["space", "non-ascii"])
def test_capture_refuses_a_folder_a_file_url_would_spell_differently(
    home: Path, jev: FakeJev, tmp_path: Path, name: str
) -> None:
    """Ruling F42: a replay's editable install records the clone's path as a
    percent-encoded `file://` URL, so the fork-check folder's absolute path
    must read the same once encoded. Such a folder is refused with a one-line
    reason, and the config is left as it was."""
    assert run(home, jev, "mode", "shadow").returncode == 0
    before = (home / "config.json").read_text()
    result = run(home, jev, "mode", "capture", "--dir", str(tmp_path / name))
    assert result.returncode == 1
    assert len(result.stdout.strip().splitlines()) == 1
    assert str(tmp_path / name) in result.stdout and "file://" in result.stdout
    assert (home / "config.json").read_text() == before


def test_capture_mode_records_when_capture_started(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    """Ruling F8: the fork check's report period starts here. Running `mode
    capture` again while capturing (to move the folder) keeps the first time."""
    assert run(home, jev, "mode", "shadow").returncode == 0
    assert "capture_started" not in json.loads((home / "config.json").read_text())
    assert run(home, jev, "mode", "capture").returncode == 0
    started = json.loads((home / "config.json").read_text())["capture_started"]
    assert datetime.fromisoformat(started).utcoffset() == timedelta(0)
    config = json.loads((home / "config.json").read_text())
    (home / "config.json").write_text(json.dumps({**config, "capture_started": "2026-10-01T08:00:00+00:00"}))
    assert run(home, jev, "mode", "capture", "--dir", str(tmp_path / "fc")).returncode == 0
    assert json.loads((home / "config.json").read_text())["capture_started"] == "2026-10-01T08:00:00+00:00"


def test_capture_mode_snapshots_a_job_it_would_delegate(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=T",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    switch_on(home, jev, mode="capture", fork_check_dir=str(tmp_path / "fc"))
    jev.raw_answers = steps_answers(3.5)
    transcript = home.parent / "claude-transcript.jsonl"
    transcript.write_text(json.dumps(assistant("r1", read=800_000, text="Ready.")) + "\n")
    calibration = home.parent / "calibration.json"
    calibration.write_text(json.dumps(CALIBRATION))
    payload = {"prompt": "build it", "session_id": "sess-1", "transcript_path": str(transcript), "cwd": str(repo)}
    result = run(
        home,
        jev,
        "hook",
        "claude",
        stdin=json.dumps(payload),
        env={"JEV_ROUTER_CALIBRATION": str(calibration), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )
    assert result.stdout == ""
    (event,) = log(home)
    assert (tmp_path / "fc" / "snapshots" / event["snapshot"] / "meta.json").exists()


def test_capture_mode_records_a_failed_snapshot_without_blocking_the_message(
    home: Path, jev: FakeJev, tmp_path: Path
) -> None:
    """A snapshot fails outside a git repository; the hook still prints nothing in
    capture mode, and the event notes the failure instead of a snapshot id."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    switch_on(home, jev, mode="capture", fork_check_dir=str(tmp_path / "fc"))
    jev.raw_answers = steps_answers(3.5)
    transcript = home.parent / "claude-transcript.jsonl"
    transcript.write_text(json.dumps(assistant("r1", read=800_000, text="Ready.")) + "\n")
    calibration = home.parent / "calibration.json"
    calibration.write_text(json.dumps(CALIBRATION))
    payload = {"prompt": "build it", "session_id": "sess-1", "transcript_path": str(transcript), "cwd": str(not_a_repo)}
    result = run(
        home,
        jev,
        "hook",
        "claude",
        stdin=json.dumps(payload),
        env={"JEV_ROUTER_CALIBRATION": str(calibration), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )
    assert result.stdout == ""
    (event,) = log(home)
    assert event.get("snapshot_error") == "git rev-parse failed" and "snapshot" not in event


# --- status ------------------------------------------------------------------------


def test_status_counts_sizes_and_cost(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    for answer in (
        {"size": "tiny", "confidence": 90, "follow_up": False},
        {"size": "tiny", "confidence": 90, "follow_up": True},
        {"size": "hardest", "confidence": 40, "follow_up": False},
    ):
        jev.answer = answer
        hook(home, jev)
    jev.delay, jev.answer = 5, {"size": "tiny", "confidence": 90, "follow_up": False}
    switch_on(home, jev, timeout_seconds=0.3)
    hook(home, jev)
    text = run(home, jev, "status").stdout
    assert "Jev router is ON" in text
    assert re.search(r"tiny\s+2\s+1", text)
    assert re.search(r"hardest\s+1\s+0", text)
    assert "1 follow-up replies, 1 under 60% sure" in text
    assert "1 timed out, 0 errors" in text
    assert "$0.0001 over 3 answered calls, plus 1 timed-out calls" in text


def test_status_keeps_classify_out_of_the_message_counts_but_in_the_cost(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, timeout_seconds=0.5)
    assert run(home, jev, "classify", "rename foo").returncode == 0
    jev.delay = 5
    table = run(home, jev, "classify", "rename bar").stdout
    assert "TimeoutError" in table and "carries on without Jev" in table
    text = run(home, jev, "status").stdout
    assert "Messages Jev sized: 0" in text
    assert "over 1 answered calls (1 of them from classify), plus 1 timed-out calls" in text


def test_status_when_nothing_happened_yet(home: Path, jev: FakeJev) -> None:
    text = run(home, jev, "status").stdout
    assert "Jev router is OFF. Turn it on with: /jev on" in text
    assert "Messages Jev sized: 0" in text


def test_status_names_the_claude_code_mode_before_any_decision(home: Path, jev: FakeJev, tmp_path: Path) -> None:
    assert "Claude Code mode: shadow." in run(home, jev, "status").stdout
    assert run(home, jev, "mode", "capture").returncode == 0
    assert f"Claude Code mode: capture. Snapshots go under {home / 'fork-check'}." in run(home, jev, "status").stdout
    assert run(home, jev, "mode", "capture", "--dir", str(tmp_path / "fc")).returncode == 0
    text = run(home, jev, "status").stdout
    assert f"Claude Code mode: capture. Snapshots go under {tmp_path / 'fc'}." in text
    assert "messages decided" not in text


def test_status_counts_claude_code_decisions(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, mode="live")
    jev.raw_answers = steps_answers(3.5)
    claude_hook(home, jev)
    jev.raw_answers = steps_answers(0.2)
    claude_hook(home, jev)
    # Final Minor 12: a message Jev never decided counts under "no opinion".
    claude_hook(home, jev, entries=[typed("hello")])
    jev.raw_answers = steps_answers(5.0)
    claude_hook(home, jev)
    text = run(home, jev, "status").stdout
    assert "Claude Code mode: live." in text
    assert "2 messages decided, 1 worth a fresh subagent, 0 snapshots. No opinion: 2." in text
    # Their size answers are not 0.2.0 routing decisions, so the size table leaves them out.
    assert re.search(r"Messages Jev sized since \S+: 0\n", text)
    assert "routed" not in text


# --- opencode: the hook module, through the repository's opencode entry -------------

OPENCODE_HARNESS = Path(__file__).resolve().parent / "jev_router_opencode_harness.mjs"


def opencode(
    home: Path,
    jev: FakeJev,
    agent: str = "build",
    prompt: str = "rename foo",
    path: str = "",
    mode: str = "tui",
    user_config: dict | None = None,
    providers: list[dict] | None = None,
    connected: list[str] | None = None,
    provider_stall_ms: int = 0,
) -> dict:
    node = shutil.which("node")
    assert node, "node is required to exercise the opencode hook"
    env = {
        **os.environ,
        "JEV_ROUTER_HOME": str(home),
        "JEV_ROUTER_API_URL": jev.url,
        "PATH": path or os.environ["PATH"],
        "JEV_TEST_OPENCODE_CONFIG": json.dumps(user_config or {}),
    }
    if providers is not None:
        env["JEV_TEST_PROVIDERS"] = json.dumps(providers)
    if connected is not None:
        env["JEV_TEST_CONNECTED"] = json.dumps(connected)
    env["JEV_TEST_PROVIDER_STALL_MS"] = str(provider_stall_ms)
    result = subprocess.run(
        [node, str(OPENCODE_HARNESS), agent, prompt, mode],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_opencode_registers_a_helper_agent_per_size_and_the_jev_command(home: Path, jev: FakeJev) -> None:
    tiers = json.loads(TIERS_FILE.read_text())["harnesses"]["opencode"]
    config = opencode(home, jev)
    for tier in tiers:
        helper = config["agent"][tier["helper"]]
        assert helper["model"] == tier["model_id"]
        assert (helper["mode"], helper["hidden"]) == ("subagent", True)
    assert "$ARGUMENTS" in config["command"]["jev"]["template"]


def test_a_stalled_opencode_server_cannot_hold_the_message(home: Path, jev: FakeJev) -> None:
    """One 8 s deadline covers opencode's own client calls too, and a late answer
    changes nothing: the message goes ahead unrouted."""
    switch_on(home, jev)
    started = time.monotonic()
    message = opencode(home, jev, provider_stall_ms=60_000)["output"]
    assert time.monotonic() - started < 11
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert len(message["parts"]) == 1


def test_opencode_never_switches_to_a_provider_declared_but_not_logged_in(home: Path, jev: FakeJev) -> None:
    """`provider.openrouter = {}` in opencode.json puts OpenRouter in config.providers()
    without credentials; only provider.list()'s `connected` tells the difference."""
    switch_on(home, jev)
    message = opencode(home, jev, connected=[])["output"]
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert len(message["parts"]) == 1


@pytest.mark.parametrize(
    "providers",
    [
        [],  # opencode has no OpenRouter login, though the router has its own key
        [{"id": "openrouter", "models": {"deepseek/deepseek-v4.1-flash": {}}}],  # tier model missing
    ],
)
def test_opencode_never_switches_to_a_model_it_cannot_run(home: Path, jev: FakeJev, providers: list[dict]) -> None:
    switch_on(home, jev)
    message = opencode(home, jev, providers=providers)["output"]
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert len(message["parts"]) == 1 and jev.requests == []


def test_opencode_helpers_keep_every_restriction_on_the_build_agent(home: Path, jev: FakeJev) -> None:
    """Routing moves a message off `build`, so a helper must not lift a limit the
    user put on build, such as denying bash."""
    limits = {"permission": {"edit": "deny", "bash": "deny"}, "tools": {"webfetch": False}, "steps": 7}
    # Model-specific settings must not follow: a reasoning budget next to the
    # tier's effort is an invalid OpenRouter request.
    model_specific = {"options": {"reasoning": {"max_tokens": 2048}}, "temperature": 0.2, "variant": "low"}
    config = opencode(home, jev, user_config={"agent": {"build": {**limits, **model_specific}}})
    for tier in json.loads(TIERS_FILE.read_text())["harnesses"]["opencode"]:
        helper = config["agent"][tier["helper"]]
        assert {k: helper[k] for k in limits} == limits
        assert not set(model_specific) & set(helper)
        assert helper["model"] == tier["model_id"] and helper["mode"] == "subagent"


def test_opencode_keeps_a_follow_up_on_the_session_model_with_a_note(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "tiny", "confidence": 99, "follow_up": True}
    message = opencode(home, jev, prompt="yes but shorter")["output"]
    assert message["message"]["agent"] == "build"
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert "add no 'Done by' line" in message["parts"][1]["text"]


def test_opencode_moves_a_routed_message_onto_the_helper_model_and_level(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    message = opencode(home, jev)["output"]
    assert message["message"]["agent"] == "jev-tiny"
    assert message["message"]["model"] == {
        "providerID": "openrouter",
        "modelID": "z-ai/glm-5.3-flash",
        "variant": "high",
    }
    user, note = message["parts"]
    assert re.fullmatch(r"prt_0e2fd8341002[0-9A-Za-z]{14}", note["id"])
    assert note["synthetic"] is True and note["messageID"] == user["messageID"]
    assert "Done by GLM 5.3 flash at high thinking" in note["text"]


@pytest.mark.parametrize("mode", ["run", "attached"])
def test_opencode_run_is_never_routed_or_sent_to_jev(home: Path, jev: FakeJev, mode: str) -> None:
    """`opencode run` is how reviews call DeepSeek with a pinned model and variant,
    including `--attach` to a server that a TUI started."""
    switch_on(home, jev)
    message = opencode(home, jev, mode=mode)["output"]
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert len(message["parts"]) == 1 and jev.requests == []


def test_opencode_leaves_the_message_alone_when_off(home: Path, jev: FakeJev) -> None:
    message = opencode(home, jev)["output"]
    assert message["message"]["agent"] == "build" and len(message["parts"]) == 1


def test_opencode_never_routes_out_of_another_agent(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    message = opencode(home, jev, agent="plan")["output"]
    assert message["message"]["agent"] == "plan" and len(message["parts"]) == 1
    assert jev.requests == []


def test_opencode_carries_on_when_the_router_cannot_run(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    message = opencode(home, jev, path="/nonexistent")["output"]
    assert message["message"]["agent"] == "build" and len(message["parts"]) == 1


def test_a_broken_jev_router_install_does_not_take_opencode_down(tmp_path: Path) -> None:
    """The entry loads every plugin; a bad tiers.json must only disable the router."""
    shutil.copytree(REPO / ".opencode", tmp_path / ".opencode")
    shutil.copytree(REPO / "task-status", tmp_path / "task-status")
    shutil.copytree(PLUGIN, tmp_path / "jev-router")
    (tmp_path / "jev-router" / "skills" / "jev" / "scripts" / "tiers.json").write_text("{")
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, str(REPO / "tests" / "opencode_entry_harness.mjs")], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    paths = json.loads(result.stdout)["config"]["skills"]["paths"]
    assert any(p.endswith("task-status/skills") for p in paths)


# --- the helpers match the tiers -----------------------------------------------------


def _frontmatter(path: Path) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", path.read_text(), re.S)
    assert match, f"{path} has no frontmatter"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def test_each_claude_tier_has_a_helper_agent_on_its_model() -> None:
    tiers = json.loads(TIERS_FILE.read_text())["harnesses"]["claude"]
    agents = sorted((PLUGIN / "agents").glob("*.md"))
    assert sorted(f"jev-router:{a.stem}" for a in agents) == sorted(t["helper"] for t in tiers)
    for tier in tiers:
        agent = PLUGIN / "agents" / f"{tier['helper'].split(':', 1)[1]}.md"
        meta = _frontmatter(agent)
        assert meta["model"] == tier["model_id"]
        assert meta.get("effort") == tier["effort"], agent
        label = f"{tier['model']} at {tier['effort']} thinking" if tier["effort"] else tier["model"]
        assert agent.read_text().rstrip().endswith(f"Done by {label}"), agent


def test_every_claude_tier_model_has_a_price() -> None:
    """The Claude route prices every helper it can pick without checking first."""
    prices = json.loads(SCRIPT.with_name("prices.json").read_text())["per_million_tokens"]
    for tier in json.loads(TIERS_FILE.read_text())["harnesses"]["claude"]:
        assert tier["model_id"] in prices, tier["size"]


def test_every_harness_has_every_size_smallest_first() -> None:
    data = json.loads(TIERS_FILE.read_text())
    for harness, tiers in data["harnesses"].items():
        assert [t["size"] for t in tiers] == list(data["jobs"]), harness


# The router only ever routes inside one harness: a Claude Code session never hands
# work to a GPT model and vice versa, which keeps the user's cross-AI review rules
# (Codex reviews Claude, Claude reviews Codex) meaningful. Each harness also only
# gets thinking levels its models accept. Haiku 4.5 has none.
HARNESS_MODELS = {
    "claude": ({"claude-haiku-4-5-20251001", "claude-opus-5-5"}, {None, "low", "medium", "high", "xhigh", "max"}),
    "codex": ({"gpt-6-luna", "gpt-6-sol", "gpt-6-astra"}, {"low", "medium", "high", "xhigh", "max"}),
    "opencode": ({"openrouter/z-ai/glm-5.3-flash", "openrouter/deepseek/deepseek-v4.1-flash"}, {"low", "high", "max"}),
}


def test_every_tier_stays_inside_its_harness_with_a_level_its_model_accepts() -> None:
    data = json.loads(TIERS_FILE.read_text())
    assert set(data["harnesses"]) == set(HARNESS_MODELS)
    for harness, tiers in data["harnesses"].items():
        models, efforts = HARNESS_MODELS[harness]
        for tier in tiers:
            assert tier["model_id"] in models, f"{harness} {tier['size']} names {tier['model_id']}"
            assert tier["effort"] in efforts, f"{harness} {tier['size']} uses effort {tier['effort']}"
            assert (tier["effort"] is None) == (tier["model_id"] == "claude-haiku-4-5-20251001")
