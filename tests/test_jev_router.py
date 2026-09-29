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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "jev-router"
SCRIPT = PLUGIN / "skills" / "jev" / "scripts" / "jev_router.py"
TIERS_FILE = SCRIPT.with_name("tiers.json")

_spec = importlib.util.spec_from_file_location("jev_router", SCRIPT)
assert _spec and _spec.loader
jev_router = importlib.util.module_from_spec(_spec)
# Reason: dataclasses look their module up in sys.modules while it executes.
sys.modules["jev_router"] = jev_router
_spec.loader.exec_module(jev_router)


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
    harness: str = "claude",
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


def test_confident_job_is_handed_to_the_matching_claude_helper(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    out = json.loads(hook(home, jev))["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "`jev-router:tiny`" in out["additionalContext"]
    assert "Claude Haiku 4.5" in out["additionalContext"]
    assert "97% sure" in out["additionalContext"]
    [event] = log(home)
    assert event["outcome"] == "routed" and event["size"] == "tiny" and event["cost"] == 0.00002
    assert "rename" not in json.dumps(event), "the log must not keep message text"


def test_sixty_percent_is_sure_enough_and_below_is_kept(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "large", "confidence": 60, "follow_up": False}
    assert "jev-router:large" in hook(home, jev)
    jev.answer = {"size": "large", "confidence": 59, "follow_up": False}
    context = json.loads(hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert "not routing this message" in context and "59% sure" in context
    assert "Carry on with it as you normally would" in context
    assert "jev-router:" not in context
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


def test_claude_hands_off_unless_the_session_is_certain_it_matches(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "large", "confidence": 90, "follow_up": False}
    context = json.loads(hook(home, jev))["hookSpecificOutput"]["additionalContext"]
    assert "`jev-router:large` subagent, which runs on Claude Opus 5.5 at high thinking" in context
    assert "If you are running on exactly Claude Opus 5.5 at high thinking, handle it yourself" in context
    assert "if you cannot tell, hand it off" in context


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
    assert hook(home, jev, attended=attended) == ""
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


def test_status_counts_an_answered_call_that_reported_no_cost(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.usage = {}
    assert "jev-router:tiny" in hook(home, jev)
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
