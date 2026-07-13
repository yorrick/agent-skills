#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
#
# Forked from https://github.com/alisorcorp/ask-local (MIT).
# Local modifications: --context flag (caller-composed system-prompt briefing),
# default model set to qwen3-coder-30b-a3b-instruct-mlx.
"""
Query a local LM Studio model via the OpenAI-compatible API.

Usage:
    query_lm.py [OPTIONS] PROMPT
    query_lm.py --list-models

Options:
    --model MODEL      Model ID (default: qwen3-coder-30b-a3b-instruct-mlx)
    --think            Enable extended reasoning (default: off for speed)
    --max-tokens N     Max response tokens (default: 1000)
    --system SYS       System prompt
    --url URL          LM Studio base URL (default: http://localhost:1234)
    --list-models      List available models and exit
    --no-stream        Disable streaming output

Notes:
    - Use --think for tasks that benefit from step-by-step reasoning.
    - Prompt can also be piped via stdin (content is prepended to the prompt).
    - Uses only Python stdlib — no pip dependencies required.
"""

import argparse
import json
import select
import sys
import urllib.error
import urllib.request
from typing import NoReturn

LMSTUDIO_URL = "http://localhost:1234"
DEFAULT_MODEL = "qwen3-coder-30b-a3b-instruct-mlx"


def list_models(base_url, timeout=5):
    req = urllib.request.Request(f"{base_url}/v1/models")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return [m["id"] for m in data.get("data", [])]


def query(prompt, model, think, max_tokens, system, base_url, stream):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": bool(stream),
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    if not stream:
        with urllib.request.urlopen(req, timeout=300) as resp:
            r = json.loads(resp.read())
        msg = r["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        usage = r.get("usage") or {}
        if think and reasoning:
            print(f"<thinking>\n{reasoning}\n</thinking>\n", file=sys.stderr)
        if content:
            print(content)
        elif reasoning:
            print(f"[Hit token limit during reasoning — try --max-tokens higher]\n\n{reasoning}")
        else:
            print("[No response content returned]")
        if usage:
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            print(f"[qwen: {p_tok:,} prompt + {c_tok:,} completion tok]")
        return

    # Streaming path
    content_parts = []
    reasoning_parts = []
    usage = {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    content_parts.append(content)
                    sys.stdout.write(content)
                    sys.stdout.flush()
                rc = delta.get("reasoning_content")
                if rc and think:
                    reasoning_parts.append(rc)
            if chunk.get("usage"):
                usage = chunk["usage"]
    if content_parts:
        sys.stdout.write("\n")
        sys.stdout.flush()
    elif reasoning_parts:
        print(f"[reasoning only]\n{''.join(reasoning_parts)}")
    else:
        print("[No response content returned]")
    if usage:
        p_tok = usage.get("prompt_tokens", 0)
        c_tok = usage.get("completion_tokens", 0)
        print(f"[qwen: {p_tok:,} prompt + {c_tok:,} completion tok]")


def die(msg, code=1) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def die_conn(base_url, e) -> NoReturn:
    print(f"Error: cannot reach LM Studio at {base_url}", file=sys.stderr)
    print(
        "  Is LM Studio running with the Local Server enabled on this port?\n"
        "  (In LM Studio: Developer tab → Start Server, port 1234 by default.)",
        file=sys.stderr,
    )
    print(f"  Details: {e.reason if hasattr(e, 'reason') else e}", file=sys.stderr)
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser(
        description="Query a local LM Studio model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("prompt", nargs="?", help="Prompt to send to the model")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--think", action="store_true", help="Enable extended reasoning")
    parser.add_argument("--max-tokens", type=int, default=1000, dest="max_tokens")
    parser.add_argument("--system", default="", help="System prompt")
    parser.add_argument("--url", default=LMSTUDIO_URL)
    parser.add_argument("--list-models", action="store_true", dest="list_models_flag")
    parser.add_argument("--no-stream", dest="stream", action="store_false", default=True)

    args = parser.parse_args()

    if args.list_models_flag:
        try:
            for m in list_models(args.url):
                print(m)
        except urllib.error.URLError as e:
            die_conn(args.url, e)
        return

    if not sys.stdin.isatty():
        # Read stdin unconditionally when it's the sole prompt source. When a
        # prompt argument was also given, wait up to 1s for the first piped
        # byte, then read to EOF — so a real (possibly slow) producer still
        # gets prepended, while a non-tty stdin left open by a subprocess
        # can only delay us by the grace window instead of blocking forever.
        stdin_content = ""
        if not args.prompt:
            stdin_content = sys.stdin.read().strip()
        elif select.select([sys.stdin], [], [], 1.0)[0]:
            stdin_content = sys.stdin.read().strip()
        if stdin_content:
            args.prompt = f"{stdin_content}\n\n{args.prompt}" if args.prompt else stdin_content
    if not args.prompt:
        parser.error("PROMPT is required (or pipe content via stdin)")

    # Preflight
    try:
        available = list_models(args.url)
    except urllib.error.URLError as e:
        die_conn(args.url, e)
    if args.model not in available:
        detail = "  Available models: " + ", ".join(available) if available else "  No models are currently loaded."
        die(f"model '{args.model}' is not loaded in LM Studio at {args.url}.\n{detail}", code=2)

    try:
        query(args.prompt, args.model, args.think, args.max_tokens, args.system, args.url, args.stream)
    except urllib.error.URLError as e:
        die_conn(args.url, e)
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
