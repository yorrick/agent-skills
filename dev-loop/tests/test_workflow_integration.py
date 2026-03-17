#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mermaid-ascii"]
# ///
"""Integration test for the workflow engine with multi-LLM orchestration.

Creates a temporary Python project with a deliberate gap, then runs a
workflow script that uses Codex to implement and Claude to review/fix.
Verifies that the implementation is correct, tests pass, and commits
were created.

Usage:
    uv run tests/test_workflow_integration.py [--no-cleanup]
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
ENGINE_PATH = SCRIPT_DIR / "engine.py"
TIMEOUT_SECONDS = 900  # 15 minutes

# --- Output helpers ---

_failures: list[str] = []
_passes: int = 0


def banner(text: str) -> None:
    print(f"\n{'=' * 64}\n  {text}\n{'=' * 64}\n", flush=True)


def ok(msg: str) -> None:
    print(f"  [OK] {msg}", flush=True)


def passed(msg: str) -> None:
    global _passes
    _passes += 1
    print(f"  [PASS] {msg}", flush=True)


def failed(msg: str, detail: str = "") -> None:
    _failures.append(msg)
    print(f"  [FAIL] {msg}", flush=True)
    if detail:
        print(f"         {detail}", flush=True)


def check(condition: bool, msg: str, detail: str = "") -> bool:
    if condition:
        passed(msg)
    else:
        failed(msg, detail)
    return condition


# --- Project scaffold ---


def scaffold_project(project_dir: Path) -> None:
    """Create a minimal Python project with room to add features."""
    (project_dir / "src" / "mathlib").mkdir(parents=True)
    (project_dir / "tests").mkdir()

    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "mathlib"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        'dependencies = ["pytest"]\n\n'
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'pythonpath = ["src"]\n'
    )

    (project_dir / "src" / "mathlib" / "__init__.py").write_text("")

    (project_dir / "src" / "mathlib" / "stats.py").write_text(
        '"""Basic statistics functions."""\n\n\n'
        "def mean(numbers: list[float]) -> float:\n"
        '    """Return the arithmetic mean."""\n'
        "    if not numbers:\n"
        '        raise ValueError("Cannot compute mean of empty list")\n'
        "    return sum(numbers) / len(numbers)\n"
    )

    (project_dir / "tests" / "__init__.py").write_text("")

    (project_dir / "tests" / "test_stats.py").write_text(
        "import pytest\n"
        "from mathlib.stats import mean\n\n\n"
        "def test_mean_basic():\n"
        "    assert mean([1, 2, 3]) == 2.0\n\n\n"
        "def test_mean_single():\n"
        "    assert mean([5]) == 5.0\n\n\n"
        "def test_mean_empty():\n"
        "    with pytest.raises(ValueError):\n"
        "        mean([])\n"
    )

    # Plan file for the workflow to implement
    plans_dir = project_dir / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "add-median-and-mode.md").write_text(
        "# Add median and mode functions\n\n"
        "## Summary\n\n"
        "Add `median` and `mode` functions to `src/mathlib/stats.py`.\n\n"
        "## Implementation\n\n"
        "### Step 1: Add median function\n\n"
        "Add to `src/mathlib/stats.py`:\n\n"
        "```python\n"
        "def median(numbers: list[float]) -> float:\n"
        '    """Return the median value. Raises ValueError if empty."""\n'
        "    if not numbers:\n"
        '        raise ValueError("Cannot compute median of empty list")\n'
        "    sorted_nums = sorted(numbers)\n"
        "    n = len(sorted_nums)\n"
        "    mid = n // 2\n"
        "    if n % 2 == 0:\n"
        "        return (sorted_nums[mid - 1] + sorted_nums[mid]) / 2\n"
        "    return sorted_nums[mid]\n"
        "```\n\n"
        "### Step 2: Add mode function\n\n"
        "```python\n"
        "def mode(numbers: list[float]) -> float:\n"
        '    """Return the most common value. Raises ValueError if empty."""\n'
        "    if not numbers:\n"
        '        raise ValueError("Cannot compute mode of empty list")\n'
        "    from collections import Counter\n"
        "    counts = Counter(numbers)\n"
        "    return counts.most_common(1)[0][0]\n"
        "```\n\n"
        "### Step 3: Add tests\n\n"
        "Add to `tests/test_stats.py`:\n"
        "- test_median_odd: median([3,1,2]) == 2.0\n"
        "- test_median_even: median([1,2,3,4]) == 2.5\n"
        "- test_median_empty: raises ValueError\n"
        "- test_mode_basic: mode([1,2,2,3]) == 2\n"
        "- test_mode_empty: raises ValueError\n\n"
        "## Validation\n\n"
        "Run `uv run pytest tests/ -v` — all tests pass.\n"
    )


def generate_workflow_script(project_dir: Path, script_path: Path) -> None:
    """Generate the multi-LLM workflow script matching Example 4 patterns."""
    script_path.write_text(
        f"""\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mermaid-ascii"]
# ///
import sys
sys.path.insert(0, {str(SCRIPT_DIR)!r})

import asyncio
from engine import StateGraph, claude_node, codex_node, shell_node, python_node, END


async def main() -> None:
    graph = StateGraph(max_iterations=5)

    # --- Implementation ---
    graph.add_node(
        "implement",
        codex_node(
            "Read the plan at docs/plans/add-median-and-mode.md and implement it exactly. "
            "Add median and mode functions to src/mathlib/stats.py. "
            "Add the specified tests to tests/test_stats.py. "
            "Do NOT modify existing functions or tests.",
            output_key="impl_output",
            cwd={str(project_dir)!r},
        ),
    )

    # --- Run tests ---
    graph.add_node(
        "run_tests",
        shell_node(
            "cd {project_dir} && uv run pytest tests/ -v 2>&1",
            output_key="test_output",
            check=False,
        ),
    )

    graph.add_node(
        "fix_tests",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "The tests failed. Read the source and test files, fix the code:\\n\\n{{test_output}}",
            output_key="fix_tests_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    def test_router(state: dict[str, str]) -> str:
        output = state.get("test_output", "")
        if "failed" in output.lower() or "error" in output.lower():
            return "fix"
        return "smoke_test"

    # --- Smoke test (context pattern: verify before review) ---
    graph.add_node(
        "smoke_test",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Run a smoke test to verify the implementation.\\n\\n"
            "1. Read docs/plans/add-median-and-mode.md. Look for a Validation section.\\n"
            "2. Execute validation instructions (run `uv run pytest tests/ -v`).\\n"
            "3. ALWAYS kill background processes before finishing.\\n\\n"
            "End with EXACTLY one line:\\n"
            "  SMOKE_TEST_PASS\\n"
            "  SMOKE_TEST_FAIL: <brief summary>",
            output_key="smoke_test_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    def smoke_test_router(state: dict[str, str]) -> str:
        error = state.get("smoke_test_error", "")
        output = state.get("smoke_test_output", "")
        if error or "SMOKE_TEST_FAIL" in output:
            return "fail"
        return "pass"

    graph.add_node(
        "smoke_test_fix",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "The smoke test failed:\\n\\n{{smoke_test_output}}\\n\\n"
            "Diagnose the root cause, fix the code, and run quality gates "
            "(lint, typecheck, format, tests). Commit fixes locally.",
            output_key="smoke_test_fix_output",
            model="sonnet",
            effort="medium",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Simplify ---
    graph.add_node(
        "simplify",
        claude_node(
            "/simplify",
            output_key="simplify_output",
            model="sonnet",
            effort="high",
            permission_mode="bypassPermissions",
        ),
    )

    graph.add_node(
        "simplify_commit",
        claude_node(
            "If there are any uncommitted changes from the simplify pass, "
            "commit them with a descriptive message.",
            output_key="simplify_commit_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Reviews (context pattern: review diff, track previous findings) ---
    graph.add_node(
        "code_review",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Review the latest changes (use `git diff HEAD~1`) for bugs, logic errors, "
            "and code quality. This was implemented by Codex — verify correctness.\\n\\n"
            "Return findings with severity (Critical/Important/Medium/Low).",
            output_key="code_review_output",
            model="opus",
            effort="high",
            permission_mode="bypassPermissions",
        ),
    )

    # Security review uses sonnet/low in test (vs opus/high in Example 4)
    # to reduce cost and runtime for integration testing.
    graph.add_node(
        "security_review",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Review the latest changes (use `git diff HEAD~1`) for security issues.\\n\\n"
            "Previous security review findings (check if resolved, then do full review):\\n"
            "{{previous_security_findings}}\\n\\n"
            "Return findings with severity (Critical/Important/Medium/Low).",
            output_key="security_review_output",
            model="sonnet",
            effort="low",
            permission_mode="bypassPermissions",
        ),
    )

    # --- Decision gate (context pattern: LLM evaluation, not regex) ---
    # Test omits wait_for_ci (no GitHub infrastructure), so no CI short-circuit.
    def _decision_fn(state: dict[str, str]) -> dict[str, str]:
        \"\"\"Carry previous_security_findings, increment iteration_count.\"\"\"
        security_text = state.get("security_review_output", "")
        iteration = int(state.get("iteration_count", "1"))
        return {{
            "decision_output": state.get("decision_llm_output", "NO"),
            "previous_security_findings": security_text,
            "iteration_count": str(iteration + 1),
        }}

    graph.add_node(
        "decision_llm",
        claude_node(
            "Based on these review findings, are there Critical, Important, or Medium "
            "severity issues that MUST be fixed?\\n\\n"
            "Code Review:\\n{{code_review_output}}\\n\\n"
            "Security Review:\\n{{security_review_output}}\\n\\n"
            "Answer EXACTLY: YES or NO. Only YES for Critical/Important/Medium. "
            "Low severity and nitpicks do not count.",
            output_key="decision_llm_output",
            model="sonnet",
            effort="low",
        ),
    )
    graph.add_node("decision", python_node(_decision_fn))

    # --- Fix (context pattern: quality gates after every fix) ---
    graph.add_node(
        "fix_reviews",
        claude_node(
            "You are working in {project_dir}.\\n\\n"
            "Fix Critical/Important/Medium issues:\\n\\n"
            "Code review:\\n{{code_review_output}}\\n\\n"
            "Security review:\\n{{security_review_output}}\\n\\n"
            "After fixing, run quality gates (lint, typecheck, format, tests). "
            "Fix any failures. Commit locally.",
            output_key="fix_reviews_output",
            model="sonnet",
            effort="medium",
            permission_mode="bypassPermissions",
        ),
    )

    def decision_router(state: dict[str, str]) -> str:
        if "YES" in state.get("decision_output", "NO").upper():
            return "issues"
        return "clean"

    # --- Commit ---
    graph.add_node(
        "commit",
        shell_node(
            'cd {project_dir} && git add -A && git diff --cached --quiet '
            '&& echo "nothing to commit" '
            '|| git commit -m "feat: add median and mode functions"',
            output_key="commit_output",
        ),
    )

    # --- Edges ---
    # Phase 1: implement → test → smoke test
    graph.add_edge("start", "implement")
    graph.add_edge("implement", "run_tests")
    graph.add_conditional_edges("run_tests", test_router, {{
        "fix": "fix_tests", "smoke_test": "smoke_test",
    }})
    graph.add_edge("fix_tests", "run_tests")
    graph.add_conditional_edges("smoke_test", smoke_test_router, {{
        "pass": "simplify", "fail": "smoke_test_fix",
    }})
    graph.add_edge("smoke_test_fix", "smoke_test")  # retry

    # Phase 2: simplify → review loop
    graph.add_edge("simplify", "simplify_commit")
    graph.add_parallel_edges("simplify_commit", ["code_review", "security_review"])
    graph.add_edge("code_review", "decision_llm")
    graph.add_edge("security_review", "decision_llm")
    graph.add_edge("decision_llm", "decision")
    graph.add_conditional_edges("decision", decision_router, {{
        "issues": "fix_reviews", "clean": "commit",
    }})
    graph.add_edge("fix_reviews", "simplify")  # loop back to review
    graph.add_edge("commit", END)

    if "--diagram" in sys.argv:
        print(graph.to_ascii())
        return

    result = await graph.run()
    print("TEST_OUTPUT:" + result.get("test_output", ""))
    print("COMMIT_OUTPUT:" + result.get("commit_output", ""))

asyncio.run(main())
"""
    )


# --- Verification ---


def verify_implementation(project_dir: Path) -> None:
    """Verify the implementation is correct."""
    stats_py = project_dir / "src" / "mathlib" / "stats.py"
    if stats_py.exists():
        content = stats_py.read_text()
        check("def median" in content, "median function exists in stats.py")
        check("def mode" in content, "mode function exists in stats.py")
        check("def mean" in content, "original mean function preserved")
    else:
        failed("stats.py not found")

    test_files = list(project_dir.glob("tests/test_*.py"))
    check(len(test_files) > 0, "Test files exist")

    test_result = subprocess.run(
        ["uv", "run", "pytest", "-x", "-q"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(
        test_result.returncode == 0,
        "All tests pass",
        test_result.stdout[-300:] if test_result.returncode != 0 else "",
    )

    # Count tests — should have at least 8 (3 original + 5 new)
    test_count_result = subprocess.run(
        ["uv", "run", "pytest", "--co", "-q"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if test_count_result.returncode == 0:
        lines = [line for line in test_count_result.stdout.strip().splitlines() if "::" in line]
        check(len(lines) >= 7, f"At least 7 tests collected (got {len(lines)})")
    else:
        failed("Could not count tests")


def verify_git(project_dir: Path) -> None:
    """Verify git state."""
    log_result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    commits = log_result.stdout.strip().splitlines()
    check(len(commits) >= 2, f"At least 2 commits (got {len(commits)})")

    # Check that the latest commit message mentions the feature
    if commits:
        latest = commits[0].lower()
        check(
            "median" in latest or "mode" in latest or "feat" in latest,
            f"Latest commit message references feature: {commits[0]}",
        )

    # Working tree clean
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    check(
        status_result.stdout.strip() == "",
        "Working tree is clean after workflow",
    )


# --- Main ---


def main() -> int:
    banner("Workflow engine integration test (multi-LLM)")

    project_dir = Path(tempfile.mkdtemp(prefix="workflow-integration-test-"))
    do_cleanup = "--no-cleanup" not in sys.argv
    if do_cleanup:
        atexit.register(lambda: shutil.rmtree(project_dir, ignore_errors=True))

    # Setup
    banner("Setup")
    scaffold_project(project_dir)
    ok(f"Created temp project at {project_dir}")

    subprocess.run(["git", "init"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=project_dir, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit: mathlib with mean()"],
        cwd=project_dir,
        capture_output=True,
    )
    ok("Git repo initialized")

    # Verify baseline tests pass
    baseline = subprocess.run(
        ["uv", "run", "pytest", "-q"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(baseline.returncode == 0, "Baseline tests pass", baseline.stdout[-200:])

    # Generate and run workflow script
    banner("Generate workflow script")
    script_path = project_dir / "workflow.py"
    generate_workflow_script(project_dir, script_path)
    ok(f"Script written to {script_path}")

    # Show diagram
    diagram_result = subprocess.run(
        ["uv", "run", str(script_path), "--diagram"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if diagram_result.returncode == 0:
        ok("Diagram generated successfully")
        print(diagram_result.stdout, flush=True)
    else:
        failed("Diagram generation failed", diagram_result.stderr[:300])

    # Execute workflow
    banner("Execute workflow")
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    start = time.monotonic()
    proc = subprocess.Popen(
        ["uv", "run", str(script_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=project_dir,
        text=True,
    )

    output_lines: list[str] = []
    while proc.poll() is None:
        elapsed = int(time.monotonic() - start)
        m, s = divmod(elapsed, 60)
        print(f"  [..] Running... (elapsed: {m}m{s:02d}s)", flush=True)

        if proc.stdout:
            import select

            while select.select([proc.stdout], [], [], 0)[0]:
                line = proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line)
                print(line, end="", flush=True)

        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pass

        if elapsed > TIMEOUT_SECONDS:
            proc.kill()
            failed("Timeout", f"Killed after {TIMEOUT_SECONDS}s")
            break

    if proc.stdout:
        for line in proc.stdout:
            output_lines.append(line)
            print(line, end="", flush=True)

    elapsed = int(time.monotonic() - start)
    m, s = divmod(elapsed, 60)
    stdout = "".join(output_lines)

    if proc.returncode == 0:
        ok(f"Workflow completed ({m}m{s:02d}s)")
    else:
        failed(
            f"Workflow failed (exit {proc.returncode}, {m}m{s:02d}s)",
            stdout[-500:] if stdout else "",
        )

    check("passed" in stdout.lower(), "Test output indicates tests passed")

    # Default progress logging — engine should emit [workflow] lines when no
    # callbacks are registered (the generated workflow script doesn't register any).
    banner("Verify default progress logging")
    check(
        "[workflow:" in stdout and "] Starting" in stdout,
        "Default progress log: [workflow:*] Starting lines present",
    )
    check(
        "[workflow:" in stdout and "] Finished" in stdout,
        "Default progress log: [workflow:*] Finished lines present",
    )
    # Spot-check node names in the logs — verify new pipeline steps
    for node_name in ["implement", "run_tests", "smoke_test", "simplify", "decision"]:
        check(
            f"[workflow:{node_name}] Starting" in stdout,
            f"Default progress log: '{node_name}' node logged",
        )

    # Verification
    banner("Verify implementation")
    verify_implementation(project_dir)

    banner("Verify git state")
    verify_git(project_dir)

    # Summary
    total = _passes + len(_failures)
    banner(f"RESULT: {_passes}/{total} passed, {len(_failures)} failed")
    if _failures:
        for f in _failures:
            print(f"  - {f}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
