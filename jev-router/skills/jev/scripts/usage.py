"""Model calls and their price, read from Claude Code transcript entries.

Shared by the hook, the shadow report and the fork check, so all three count and
price calls exactly as the study did (docs/superpowers/specs/2026-09-28-jev-router-cache-aware-design.md).
"""

from __future__ import annotations

import hashlib
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
        entry_usage = message.get("usage")
        request = entry.get("requestId") or message.get("id")
        model = str(message.get("model") or "?")
        if not isinstance(entry_usage, dict) or not request or model == "<synthetic>":
            continue
        out = _int(entry_usage.get("output_tokens"))
        if request in seen:
            i = seen[request]
            if out > found[i].out:
                found[i] = replace(found[i], out=out)
            continue
        seen[request] = len(found)
        created = _int(entry_usage.get("cache_creation_input_tokens"))
        split = entry_usage.get("cache_creation")
        w5m = _int(split.get("ephemeral_5m_input_tokens")) if isinstance(split, dict) else 0
        found.append(
            Call(
                model=canonical_model(model),
                inp=_int(entry_usage.get("input_tokens")),
                read=_int(entry_usage.get("cache_read_input_tokens")),
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


def prompt_sha(text: str) -> str:
    """The hook logs this, never the text."""
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]
