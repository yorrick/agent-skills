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
Run a tool-calling agent loop with a local LM Studio model.

The model can read files, list directories, and grep the project itself.
File contents enter the local model's context, not the outer Claude session's.

Usage:
    agent_lm.py [OPTIONS] "task description"

Options:
    --model MODEL           Model ID (default: qwen3-coder-30b-a3b-instruct-mlx)
    --dir DIR               Working directory the model can access (default: cwd)
    --max-tokens N          Max tokens per response (default: 6000 — sized for
                            Qwen 64k windows with comfortable headroom).
                            Raise to 10000-12000 if you run a 96k+ window.
    --max-turns N           Max agent loop iterations (default: 15)
    --read-budget N         Max read_file calls before tools are force-disabled.
                            list_dir and grep are free. (default: 15)
    --max-read-chars N      Per-file read truncation cap (default: 12000)
    --max-file-bytes N      Refuse to read files larger than this (default: 500000)
    --think                 Enable reasoning mode
    --url URL               LM Studio base URL (default: http://localhost:1234)
    --no-stream             Disable streaming of the final answer
    --quiet                 Suppress progress logs (turn markers, tool calls)

Example:
    agent_lm.py --dir /path/to/project "summarize solar-system.html"
    agent_lm.py --dir . "find all TODO comments and list them"
    agent_lm.py --dir . "review app.py for bugs"
"""

import argparse
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

LMSTUDIO_URL = "http://localhost:1234"
DEFAULT_MODEL = "qwen3-coder-30b-a3b-instruct-mlx"
DEFAULT_MAX_READ_CHARS = 12000
DEFAULT_MAX_FILE_BYTES = 500_000

# File extensions we refuse to read as text. Sniff for null bytes catches the rest.
BINARY_EXTS = {
    ".onnx",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".svg",
    ".mp3",
    ".mp4",
    ".wav",
    ".flac",
    ".mov",
    ".avi",
    ".mkv",
    ".ogg",
    ".m4a",
    ".pkl",
    ".bin",
    ".pt",
    ".safetensors",
    ".gguf",
    ".ggml",
    ".exe",
    ".dll",
    ".dylib",
    ".so",
    ".a",
    ".o",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    ".jar",
    ".class",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".DS_Store",
}

# Directories skipped during recursive grep.
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".next",
    ".nuxt",
    ".svelte-kit",
    "dist",
    "build",
    "out",
    "target",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".cache",
    ".turbo",
    ".parcel-cache",
    "vendor",
    "bower_components",
    ".idea",
    ".vscode",
}

MAX_GREP_MATCHES = 50
MAX_GREP_LINE_LEN = 200

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a text file in the working directory. "
                "Binary files and files larger than the per-file byte cap are refused. "
                "Reads are cached — calling this a second time on the same path returns "
                "identical content and does not consume budget."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the working directory, or absolute inside it.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories at a path in the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the working directory. Use '.' for root.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Search files under the working directory for a regex pattern (Python re syntax). "
                "Returns up to 50 matches as 'path:line:content'. Skips binary files, build dirs "
                "(node_modules, .git, dist, .venv, etc.), and files larger than the byte cap. "
                "Use this instead of reading many files when you're looking for specific symbols, "
                "strings, or patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Subdirectory to search within (default: '.').",
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional filename glob to limit search (e.g., '*.py', '*.ts'). "
                            "Omit to search all text files."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]


def _within(path, work_dir):
    """True if path (resolved) lives inside work_dir. Safe against prefix attacks."""
    try:
        path.resolve().relative_to(work_dir.resolve())
        return True
    except ValueError:
        return False


def _looks_binary(path):
    """Return a reason string if path looks binary, else None."""
    if path.suffix.lower() in BINARY_EXTS:
        return f"extension '{path.suffix}' is in the binary-file blocklist"
    try:
        with open(path, "rb") as f:
            sample = f.read(1024)
        if b"\0" in sample:
            return "file contains null bytes in the first 1KB (likely binary)"
    except OSError:
        return None
    return None


def execute_tool(name, args, work_dir, max_read_chars, max_file_bytes):
    """Execute a tool call and return the result string."""
    try:
        if name == "read_file":
            raw = args.get("path", "")
            if not raw:
                return "Error: read_file requires a 'path' argument."
            p = Path(raw)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            if not _within(p, work_dir):
                return f"Error: path '{raw}' is outside the working directory."
            if not p.exists():
                return f"Error: file '{raw}' does not exist."
            if not p.is_file():
                return f"Error: '{raw}' is not a regular file."
            try:
                size = p.stat().st_size
            except OSError as e:
                return f"Error reading '{raw}': {e}"
            if size > max_file_bytes:
                return (
                    f"Error: '{raw}' is {size:,} bytes, exceeding the {max_file_bytes:,}-byte cap. "
                    "Refusing to read. If you need a portion of this file, use grep instead."
                )
            binary_reason = _looks_binary(p)
            if binary_reason:
                return (
                    f"Error: refusing to read '{raw}' — {binary_reason}. Use grep if you need to search for patterns."
                )
            content = p.read_text(errors="replace")
            if len(content) > max_read_chars:
                head = max_read_chars * 6 // 10
                # Clamp: for tiny --max-read-chars, a negative tail slice would
                # silently return most of the file instead of truncating.
                tail = max(max_read_chars - head - 40, 0)
                marker = f"\n\n...[truncated — file is {len(content):,} chars]...\n\n"
                content = content[:head] + marker + (content[-tail:] if tail else "")
            return content

        if name == "list_dir":
            raw = args.get("path", ".")
            p = Path(raw)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            if not _within(p, work_dir):
                return f"Error: path '{raw}' is outside the working directory."
            if not p.is_dir():
                return f"Error: '{raw}' is not a directory."
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
            lines = []
            for e in entries:
                kind = "DIR " if e.is_dir() else "FILE"
                lines.append(f"{kind}  {e.name}")
            return "\n".join(lines) if lines else "(empty)"

        if name == "grep":
            pattern = args.get("pattern", "")
            if not pattern:
                return "Error: grep requires a 'pattern' argument."
            try:
                regex = re.compile(pattern)
            except re.error as e:
                return f"Error: invalid regex '{pattern}': {e}"
            raw = args.get("path", ".")
            glob_pat = args.get("glob")
            p = Path(raw)
            if not p.is_absolute():
                p = work_dir / p
            p = p.resolve()
            if not _within(p, work_dir):
                return f"Error: path '{raw}' is outside the working directory."
            if not p.is_dir():
                return f"Error: '{raw}' is not a directory."

            matches = []
            work_resolved = work_dir.resolve()
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fname in filenames:
                    if fname.startswith("."):
                        continue
                    if glob_pat and not fnmatch.fnmatch(fname, glob_pat):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        # A symlinked file can point outside the working directory;
                        # enforce the same containment guarantee as read_file.
                        resolved = fpath.resolve()
                        if not _within(resolved, work_dir):
                            continue
                        if resolved.stat().st_size > max_file_bytes:
                            continue
                        if resolved.suffix.lower() in BINARY_EXTS:
                            continue
                        with open(resolved, "rb") as f:
                            sample = f.read(512)
                        if b"\0" in sample:
                            continue
                        with open(resolved, errors="replace") as f:
                            for lineno, line in enumerate(f, 1):
                                if regex.search(line):
                                    try:
                                        rel = fpath.relative_to(work_resolved)
                                    except ValueError:
                                        rel = fpath
                                    trimmed = line.rstrip("\n")
                                    if len(trimmed) > MAX_GREP_LINE_LEN:
                                        trimmed = trimmed[:MAX_GREP_LINE_LEN] + "…"
                                    matches.append(f"{rel}:{lineno}:{trimmed}")
                                    if len(matches) >= MAX_GREP_MATCHES:
                                        break
                    except (OSError, UnicodeDecodeError):
                        continue
                    if len(matches) >= MAX_GREP_MATCHES:
                        break
                if len(matches) >= MAX_GREP_MATCHES:
                    break

            if not matches:
                return f"No matches for /{pattern}/ in {raw}."
            trailer = (
                f"\n[truncated at {MAX_GREP_MATCHES} matches — narrow the pattern or path]"
                if len(matches) >= MAX_GREP_MATCHES
                else ""
            )
            return "\n".join(matches) + trailer

        return f"Error: unknown tool '{name}'"
    except Exception as e:
        return f"Error executing {name}: {e}"


def list_available_models(base_url, timeout=5):
    """Return list of model IDs reported by LM Studio, or raise urllib.error.URLError."""
    req = urllib.request.Request(f"{base_url}/v1/models")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return [m["id"] for m in data.get("data", [])]


def chat(messages, model, max_tokens, think, base_url, with_tools=True, stream=False, on_content_chunk=None):
    """Send messages to LM Studio and return a response dict (OpenAI shape).

    If stream=True, content chunks are passed to on_content_chunk as they arrive.
    The returned dict is assembled from the stream to match the non-streaming shape.
    """
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.5,
        "stream": bool(stream),
        "chat_template_kwargs": {"enable_thinking": bool(think)},
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    if with_tools:
        body["tools"] = TOOLS
        body["tool_choice"] = "auto"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    if not stream:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())

    # Streaming path: parse SSE, assemble final response.
    content_parts = []
    tool_calls_acc = {}
    finish_reason = None
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
                    if on_content_chunk:
                        on_content_chunk(content)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = tool_calls_acc.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
            if chunk.get("usage"):
                usage = chunk["usage"]

    content_str = "".join(content_parts)
    tool_calls_list = [tool_calls_acc[k] for k in sorted(tool_calls_acc.keys())] if tool_calls_acc else None
    msg: dict[str, object] = {"role": "assistant"}
    if content_str:
        msg["content"] = content_str
    if tool_calls_list:
        msg["tool_calls"] = tool_calls_list
    return {
        "choices": [{"message": msg, "finish_reason": finish_reason}],
        "usage": usage,
    }


def run_agent(
    task,
    model,
    work_dir,
    max_tokens,
    max_turns,
    think,
    base_url,
    read_budget,
    max_read_chars,
    max_file_bytes,
    stream,
    quiet,
    context="",
):
    def log(line):
        if not quiet:
            print(line, flush=True)

    header = context.strip() if context.strip() else "You are a coding assistant."
    system_prompt = (
        f"{header}\n"
        "\n"
        f"You are working in: {work_dir}\n"
        f"Tools: read_file, list_dir, grep. Budget: {read_budget} file reads "
        "(list_dir and grep are free).\n"
        "\n"
        "Rules:\n"
        "- Prefer depth over breadth. Read the smallest set of files that answers the task.\n"
        "- Use grep when looking for a specific symbol, string, or pattern — it's free "
        "and cheaper than reading many files blindly.\n"
        "- Batch related tool calls into a single turn when you can.\n"
        "- Do not re-list directories you already explored.\n"
        "- Do not re-read files you already read. Reads are cached; truncation is "
        "deterministic — a second read returns identical content.\n"
        "- After roughly 60% of your budget, stop gathering and start synthesizing.\n"
        "- When you can answer, stop calling tools and write the final answer as content.\n"
        "- Answer with what you have. Do not apologize for missing context."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    reads_made = 0
    budget_exhausted = False
    reads_cache = {}  # absolute path -> {"content": str, "turn": int}

    peak_prompt = 0
    peak_prompt_turn = 0
    sum_completion = 0
    turns_completed = 0
    final_truncated = False  # True if the final-answer turn hit max_tokens

    def stats_line():
        if turns_completed == 0:
            return ""
        warnings = []
        if budget_exhausted:
            warnings.append(
                f"[WARNING: read budget exhausted at --read-budget={read_budget}. "
                f"Qwen couldn't read all the files this task needed — the answer "
                f"above covers only the {reads_made} files it managed to read. "
                "Re-run with a higher --read-budget or narrow the task scope.]"
            )
        if final_truncated:
            warnings.append(
                f"[WARNING: output was truncated at --max-tokens={max_tokens}. "
                "Re-run with a higher --max-tokens (e.g. --max-tokens 10000) or "
                "narrow the task scope. The answer above is incomplete.]"
            )
        stats = (
            f"[qwen: peak {peak_prompt:,} prompt tok @ turn {peak_prompt_turn} | "
            f"{sum_completion:,} completion across {turns_completed} turn(s)]"
        )
        if warnings:
            return stats + "\n" + "\n".join(warnings)
        return stats

    for turn in range(max_turns):
        log(f"[turn {turn + 1}]")

        # Stream content deltas live on every turn: tool-call turns rarely
        # carry content, and the final answer streams as it generates.
        should_stream = stream

        def _print_chunk(s):
            sys.stdout.write(s)
            sys.stdout.flush()

        on_chunk = _print_chunk if should_stream else None

        r = chat(
            messages,
            model,
            max_tokens,
            think,
            base_url,
            with_tools=not budget_exhausted,
            stream=should_stream,
            on_content_chunk=on_chunk,
        )
        msg = r["choices"][0]["message"]
        finish = r["choices"][0]["finish_reason"]

        usage = r.get("usage") or {}
        prompt_tok = usage.get("prompt_tokens", 0)
        completion_tok = usage.get("completion_tokens", 0)
        if prompt_tok > peak_prompt:
            peak_prompt = prompt_tok
            peak_prompt_turn = turn + 1
        sum_completion += completion_tok
        turns_completed += 1

        messages.append(msg)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls or finish == "stop" or budget_exhausted:
            content = (msg.get("content") or "").strip()
            if finish == "length":
                final_truncated = True
            if should_stream and content:
                # Content was streamed to stdout already. End with a newline
                # separator and return "" so main() doesn't print it twice.
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "", stats_line()
            if content:
                return content, stats_line()
            reasoning = (msg.get("reasoning_content") or "").strip()
            if reasoning:
                return f"[reasoning only]\n{reasoning}", stats_line()
            return "[No response]", stats_line()

        # Tool-call turn: if content chunks were streamed, separate them from
        # the upcoming progress logs.
        if should_stream and msg.get("content"):
            sys.stdout.write("\n")
            sys.stdout.flush()

        # Execute tool calls
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            log(f"  → {name}({args})")

            result = ""
            cache_hit = False
            cache_key = None
            if name == "read_file":
                try:
                    raw = Path(args.get("path", ""))
                    cache_key = str((raw if raw.is_absolute() else work_dir / raw).resolve())
                except Exception:
                    cache_key = None
                if cache_key and cache_key in reads_cache:
                    cached = reads_cache[cache_key]
                    note = (
                        f"[Already read at turn {cached['turn']}. "
                        "Truncation is deterministic — identical content follows. "
                        "Do not request this file again.]\n\n"
                    )
                    result = note + cached["content"]
                    log("    (cache hit — no budget charge)")
                    cache_hit = True

            if not cache_hit:
                # Enforce the budget per read, not per turn: a batch of reads
                # in one turn must not overshoot the documented hard cap.
                if name == "read_file" and reads_made >= read_budget:
                    result = (
                        f"Error: read budget of {read_budget} file reads is exhausted — this read "
                        "was refused. Produce your final answer using what you have already read."
                    )
                else:
                    result = execute_tool(name, args, work_dir, max_read_chars, max_file_bytes)
                    if name == "read_file":
                        reads_made += 1
                        if cache_key:
                            reads_cache[cache_key] = {"content": result, "turn": turn + 1}

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                }
            )

        if reads_made >= read_budget and not budget_exhausted:
            budget_exhausted = True
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"BUDGET EXHAUSTED: you have made {reads_made} file reads "
                        f"(budget: {read_budget}). Tools are now disabled. "
                        "Produce your final answer now using only what you have read."
                    ),
                }
            )
            log(f"[budget exhausted after {reads_made} file reads — tools disabled]")

    return "[Agent reached max turns without a final answer]", stats_line()


def die(msg, code=1) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)


def main():
    parser = argparse.ArgumentParser(
        description="Tool-calling agent loop with a local LM Studio model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("task", help="Task description for the agent")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dir", default=".", help="Working directory (default: cwd)")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=6000,
        dest="max_tokens",
        help="Max tokens per response (default: 6000, sized for 64k windows)",
    )
    parser.add_argument("--max-turns", type=int, default=15, dest="max_turns")
    parser.add_argument(
        "--read-budget",
        type=int,
        default=15,
        dest="read_budget",
        help="Max read_file calls before tools are force-disabled. list_dir and grep are free. (default: 15)",
    )
    parser.add_argument(
        "--max-read-chars",
        type=int,
        default=DEFAULT_MAX_READ_CHARS,
        dest="max_read_chars",
        help=f"Per-file read truncation cap (default: {DEFAULT_MAX_READ_CHARS})",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        dest="max_file_bytes",
        help=f"Refuse to read files larger than this many bytes (default: {DEFAULT_MAX_FILE_BYTES})",
    )
    parser.add_argument(
        "--context",
        default="",
        help="Caller-composed briefing prepended to the system prompt: role, project/session "
        "context the model can't discover from files, and expected output format.",
    )
    parser.add_argument("--think", action="store_true")
    parser.add_argument("--url", default=LMSTUDIO_URL)
    parser.add_argument(
        "--no-stream", dest="stream", action="store_false", default=True, help="Disable streaming of the final answer"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress logs (turn markers, tool calls)")

    args = parser.parse_args()
    work_dir = Path(args.dir).resolve()
    if not work_dir.is_dir():
        die(f"--dir '{args.dir}' is not a valid directory")

    # Preflight: LM Studio reachable + requested model loaded.
    try:
        available = list_available_models(args.url)
    except urllib.error.URLError as e:
        die(
            f"cannot reach LM Studio at {args.url}\n"
            "  Is LM Studio running with the Local Server enabled on this port?\n"
            "  (In LM Studio: Developer tab → Start Server, port 1234 by default.)\n"
            f"  Details: {e.reason if hasattr(e, 'reason') else e}",
            code=2,
        )
    if args.model not in available:
        if available:
            detail = (
                "  Available models: " + ", ".join(available) + "\n"
                "  Either load the requested model in LM Studio, or pass --model with one of the above."
            )
        else:
            detail = "  No models are currently loaded. Load one in LM Studio first."
        die(f"model '{args.model}' is not loaded in LM Studio at {args.url}.\n{detail}", code=2)

    try:
        content, stats = run_agent(
            task=args.task,
            model=args.model,
            work_dir=work_dir,
            max_tokens=args.max_tokens,
            max_turns=args.max_turns,
            think=args.think,
            base_url=args.url,
            read_budget=args.read_budget,
            max_read_chars=args.max_read_chars,
            max_file_bytes=args.max_file_bytes,
            stream=args.stream,
            quiet=args.quiet,
            context=args.context,
        )
    except urllib.error.URLError as e:
        die(
            f"LM Studio request failed: {e.reason if hasattr(e, 'reason') else e}. "
            "The model may have been unloaded mid-run, or the request timed out.",
            code=3,
        )
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(130)

    if content:
        print(content)
    if stats:
        print(stats)


if __name__ == "__main__":
    main()
