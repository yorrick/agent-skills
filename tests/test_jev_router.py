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


class FakeJev:
    """A local OpenRouter stand-in that answers with whatever the test sets."""

    def __init__(self) -> None:
        self.answer: dict | str = {"size": "tiny", "confidence": 97, "follow_up": False}
        self.status = 200
        self.delay = 0.0
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                time.sleep(fake.delay)
                content = fake.answer if isinstance(fake.answer, str) else json.dumps(fake.answer)
                body = json.dumps(
                    {
                        "model": "openai/gpt-6-luna",
                        "choices": [{"message": {"content": content}}],
                        "usage": {"cost": 0.00002},
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
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1/chat/completions"
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


def run(home: Path, jev: FakeJev, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {"JEV_ROUTER_HOME": str(home), "JEV_ROUTER_API_URL": jev.url, "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], input=stdin, env=env, capture_output=True, text=True, timeout=30
    )


def switch_on(home: Path, jev: FakeJev, **extra: object) -> None:
    assert run(home, jev, "on", "--key-file", str(home.parent / "openrouter.sh")).returncode == 0
    if extra:
        config = json.loads((home / "config.json").read_text())
        (home / "config.json").write_text(json.dumps({**config, **extra}))


def hook(home: Path, jev: FakeJev, harness: str = "claude", prompt: str = "rename foo to bar") -> str:
    result = run(home, jev, "hook", harness, stdin=json.dumps({"prompt": prompt, "session_id": "s"}))
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
    assert "handle this message yourself" in context and "59% sure" in context
    assert "jev-router:" not in context
    assert [e["outcome"] for e in log(home)] == ["routed", "unsure"]


def test_follow_up_replies_stay_in_the_session_without_a_sign_off(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "tiny", "confidence": 99, "follow_up": True}
    context = json.loads(hook(home, jev, prompt="yes do that but make it shorter"))["hookSpecificOutput"]
    assert "follow-up" in context["additionalContext"]
    assert "add no 'Done by' line" in context["additionalContext"]
    assert log(home)[-1]["outcome"] == "follow_up"


def test_commands_and_skill_calls_are_not_sent_to_jev(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    for prompt in ("/jev off", "$jev status", "   "):
        assert hook(home, jev, prompt=prompt) == ""
    assert jev.requests == []


def test_jev_sees_the_sizes_and_a_truncated_message(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    hook(home, jev, prompt="x" * 10_000)
    [request] = jev.requests
    assert request["model"] == "typesafe/jev-router"
    system, user = request["messages"]
    for size in ("tiny", "everyday", "large", "hardest"):
        assert f"- {size}:" in system["content"]
    assert len(user["content"]) == jev_router.MAX_MESSAGE_CHARS


def test_codex_gets_three_sizes_and_spawns_with_a_model(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "everyday", "confidence": 90, "follow_up": False}
    context = json.loads(hook(home, jev, "codex"))["hookSpecificOutput"]["additionalContext"]
    assert 'spawn_agent with model "gpt-6-luna"' in context
    assert "Done by GPT-6 Luna" in context
    system = jev.requests[0]["messages"][0]["content"]
    assert "- tiny:" not in system
    assert "- everyday: a lookup, a rename, a one-line answer; or a normal email" in system


def test_opencode_gets_a_switch_it_can_apply(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    out = json.loads(hook(home, jev, "opencode"))
    assert out["agent"] == "jev-tiny"
    assert out["model_id"] == "openrouter/anthropic/claude-haiku-4.5"
    assert "Done by Claude Haiku 4.5" in out["context"]


# --- never in the way ----------------------------------------------------------------


def test_slow_jev_is_abandoned_at_the_deadline(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev, timeout_seconds=0.5)
    jev.delay = 5
    started = time.monotonic()
    assert hook(home, jev) == ""
    assert time.monotonic() - started < 3
    assert log(home)[-1]["outcome"] == "timeout"


@pytest.mark.parametrize(
    "answer",
    ["I think this is small.", '{"size": "medium", "confidence": 90}', '{"size": "tiny", "confidence": 250}'],
)
def test_unusable_answers_are_ignored(home: Path, jev: FakeJev, answer: str) -> None:
    switch_on(home, jev)
    jev.answer = answer
    assert hook(home, jev) == ""
    assert log(home)[-1]["outcome"] == "error"


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
        {"size": "tiny", "confidence": 90},
        {"size": "tiny", "confidence": 90, "follow_up": True},
        {"size": "hardest", "confidence": 40},
    ):
        jev.answer = answer
        hook(home, jev)
    jev.delay, jev.answer = 5, {"size": "tiny", "confidence": 90}
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


def opencode(home: Path, jev: FakeJev, agent: str = "build", prompt: str = "rename foo", path: str = "") -> dict:
    node = shutil.which("node")
    assert node, "node is required to exercise the opencode hook"
    env = {
        **os.environ,
        "JEV_ROUTER_HOME": str(home),
        "JEV_ROUTER_API_URL": jev.url,
        "PATH": path or os.environ["PATH"],
    }
    result = subprocess.run(
        [node, str(OPENCODE_HARNESS), agent, prompt], cwd=REPO, env=env, capture_output=True, text=True, timeout=60
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


def test_opencode_keeps_a_follow_up_on_the_session_model_with_a_note(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    jev.answer = {"size": "tiny", "confidence": 99, "follow_up": True}
    message = opencode(home, jev, prompt="yes but shorter")["output"]
    assert message["message"]["agent"] == "build"
    assert message["message"]["model"]["modelID"] == "deepseek/deepseek-v4.1-flash"
    assert "add no 'Done by' line" in message["parts"][1]["text"]


def test_opencode_moves_a_routed_message_onto_the_helper_and_its_model(home: Path, jev: FakeJev) -> None:
    switch_on(home, jev)
    message = opencode(home, jev)["output"]
    assert message["message"]["agent"] == "jev-tiny"
    assert message["message"]["model"] == {"providerID": "openrouter", "modelID": "anthropic/claude-haiku-4.5"}
    user, note = message["parts"]
    assert re.fullmatch(r"prt_0e2fd8341002[0-9A-Za-z]{14}", note["id"])
    assert note["synthetic"] is True and note["messageID"] == user["messageID"]
    assert "Done by Claude Haiku 4.5" in note["text"]


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
        assert agent.read_text().rstrip().endswith(f"Done by {tier['model']}"), agent


def test_tiers_run_smallest_first_and_cover_every_size() -> None:
    data = json.loads(TIERS_FILE.read_text())
    order = list(data["jobs"])
    for harness, tiers in data["harnesses"].items():
        covered = [s for t in tiers for s in (*t.get("also", []), t["size"])]
        assert covered == order, f"{harness} must cover every size once, smallest first"
