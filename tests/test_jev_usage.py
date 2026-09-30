"""Calls and prices read from Claude Code transcript entries."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import usage  # noqa: E402


def assistant(
    request: str,
    *,
    model: str = "claude-opus-5-5",
    inp: int = 10,
    read: int = 1000,
    w1h: int = 50,
    w5m: int = 0,
    out: int = 20,
    sidechain: bool = False,
    text: str | None = None,
) -> dict:
    content = [{"type": "text", "text": text}] if text else [{"type": "tool_use", "name": "Read", "input": {}}]
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "requestId": request,
        "message": {
            "id": f"msg-{request}",
            "model": model,
            "content": content,
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": read,
                "cache_creation_input_tokens": w1h + w5m,
                "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m},
                "output_tokens": out,
            },
        },
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


def test_prompt_sha_ignores_surrounding_whitespace() -> None:
    assert usage.prompt_sha("  hello \n") == usage.prompt_sha("hello")


def test_prompt_sha_is_sixteen_lowercase_hex_chars() -> None:
    digest = usage.prompt_sha("hello")
    assert len(digest) == 16
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_prompt_sha_differs_for_different_texts() -> None:
    assert usage.prompt_sha("hello") != usage.prompt_sha("goodbye")
