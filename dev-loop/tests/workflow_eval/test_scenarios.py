"""Workflow generation eval runner.

Discovers scenarios under scenarios/, generates workflow scripts via
headless Claude with the dev-loop plugin, imports the graph, and runs
scenario-specific assertions.

Usage:
    uv run pytest dev-loop/tests/workflow_eval/test_scenarios.py -v
    uv run pytest -m eval -v
    uv run pytest -m eval -k bugfix_bare -v -s
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

# Ensure engine and helpers are importable
PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import StateGraph  # noqa: E402
from helpers import GraphInspector  # noqa: E402

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


def discover_scenarios() -> list[Path]:
    """Find all scenarios/*/scenario.toml, return parent dirs."""
    return sorted(p.parent for p in SCENARIOS_DIR.glob("*/scenario.toml"))


def scaffold_repo(scenario_dir: Path, tmp_path: Path) -> Path:
    """Copy scenario's repo/ tree to tmp_path, run git init."""
    repo_src = scenario_dir / "repo"
    repo_dst = tmp_path / "repo"
    if repo_src.exists():
        shutil.copytree(repo_src, repo_dst)
    else:
        repo_dst.mkdir()

    # Init git so workflow scripts can use git commands
    subprocess.run(["git", "init"], cwd=repo_dst, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dst, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial", "--allow-empty"],
        cwd=repo_dst,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )
    return repo_dst


def _extract_workflow_path(claude_output: str) -> Path | None:
    """Extract the workflow script path from Claude's JSON output."""
    try:
        data = json.loads(claude_output)
        result_text = data.get("result", "")
    except (json.JSONDecodeError, AttributeError):
        result_text = claude_output

    # Look for /tmp/workflow_NNNN.py pattern in the output
    match = re.search(r"/tmp/workflow_\w+\.py", result_text)
    if match:
        path = Path(match.group())
        if path.exists():
            return path
    return None


def generate_workflow(prompt: str, repo_path: Path) -> Path:
    """Generate a workflow script via headless Claude with the dev-loop plugin.

    Returns path to the generated workflow.py file.
    """
    combined_prompt = (
        f"Use /workflow to generate a workflow for the following task. "
        f"Generate the script only, do not execute it.\n\n"
        f"Task: {prompt}"
    )

    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "ANTHROPIC_API_KEY")}

    result = subprocess.run(
        [
            "claude",
            "-p",
            combined_prompt,
            "--model",
            "sonnet",
            "--permission-mode",
            "bypassPermissions",
            "--plugin-dir",
            str(PLUGIN_DIR),
            "--output-format",
            "json",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,  # 5 minute timeout
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Claude failed to generate workflow (exit {result.returncode}):\nstderr: {result.stderr[:1000]}"
        )

    # Extract the workflow path from Claude's output
    workflow_path = _extract_workflow_path(result.stdout)
    if workflow_path is None:
        raise RuntimeError(f"No workflow file found in Claude output:\n{result.stdout[:2000]}")

    return workflow_path


def import_graph(workflow_path: Path) -> StateGraph:
    """Import build_graph() from the generated script and return the graph.

    Sets up sys.path and env so the script's imports resolve.
    """
    # Set CLAUDE_PLUGIN_ROOT so the script's sys.path.insert resolves.
    # Restore the original value (or unset) after import.
    old_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)

    # Ensure engine is importable from the script's perspective
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    try:
        # Safety check: verify the script has build_graph() and if __name__ guard
        # before importing. If not, the import would execute the workflow.
        script_text = workflow_path.read_text()
        if "def build_graph" not in script_text:
            raise RuntimeError(
                f"Generated script {workflow_path} does not contain a build_graph() function. "
                f"Cannot import safely. First 500 chars:\n{script_text[:500]}"
            )
        if "if __name__" not in script_text:
            raise RuntimeError(
                f"Generated script {workflow_path} has no if __name__ guard. Importing would execute the workflow."
            )

        spec = importlib.util.spec_from_file_location("workflow_gen", workflow_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load module from {workflow_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        build_graph = getattr(module, "build_graph", None)
        if build_graph is None:
            raise RuntimeError(f"Generated script {workflow_path} does not have a build_graph() function")

        # Pass all models as available for deterministic topology
        return build_graph(models={"claude": True, "codex": True, "gemini": True})
    finally:
        if old_plugin_root is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old_plugin_root


scenarios = discover_scenarios()


@pytest.mark.eval
@pytest.mark.parametrize(
    "scenario_dir",
    scenarios,
    ids=lambda p: p.name,
)
def test_workflow_scenario(scenario_dir: Path, tmp_path: Path) -> None:
    """Generate a workflow for the scenario and run structural assertions."""
    with open(scenario_dir / "scenario.toml", "rb") as f:
        config = tomllib.load(f)

    repo_path = scaffold_repo(scenario_dir, tmp_path)
    workflow_path = generate_workflow(config["prompt"], repo_path)

    # Save a copy for inspection
    results_dir = SCENARIOS_DIR.parent / "results"
    results_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    saved = results_dir / f"{scenario_dir.name}_{timestamp}.py"
    shutil.copy2(workflow_path, saved)

    graph = import_graph(workflow_path)
    inspector = GraphInspector(graph)

    # Load and run scenario-specific assertions
    assertions_path = scenario_dir / "assertions.py"
    if not assertions_path.exists():
        pytest.fail(f"No assertions.py in {scenario_dir}")

    spec = importlib.util.spec_from_file_location("assertions", assertions_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"Cannot load assertions from {assertions_path}")
    assertions_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(assertions_module)

    assert_fn = getattr(assertions_module, "assert_workflow", None)
    if assert_fn is None:
        pytest.fail(f"assertions.py in {scenario_dir} missing assert_workflow()")

    assert_fn(graph, inspector)
