"""Results go to a private copy, with a PR that shows exactly keep versus delegate."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "jev-router" / "skills" / "jev" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fork_check  # noqa: E402
import publish  # noqa: E402
import replay  # noqa: E402
import snapshot  # noqa: E402
from test_jev_snapshot import EVENT, git, payload, write  # noqa: E402
from test_jev_usage import assistant, typed  # noqa: E402


def _setup(tmp_path: Path, repo: Path, snap: Path) -> tuple[Path, Path, Path, str, dict]:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    (keep / "RESULT.txt").write_text("keep\n")
    (delegate / "RESULT.txt").write_text("delegate\n")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    sid = json.loads((snap / "meta.json").read_text())["id"]
    result = {
        "id": sid,
        "sides": {
            "keep": {"clone": str(keep), "cost": 1.0, "wall_seconds": 60, "calls": 10, "delegated": False},
            "delegate": {"clone": str(delegate), "cost": 0.5, "wall_seconds": 50, "calls": 12, "delegated": True},
        },
    }
    return keep, delegate, remote, sid, result


def _gh(calls: list[tuple[str, ...]], visibility: str = "not found", pr_url: str = "https://x/pull/1\n"):
    def gh(*args: str) -> str:
        calls.append(args)
        if args[:2] == ("api", "user"):
            return "yorrick\n"
        if args[:2] == ("repo", "view"):
            if visibility == "not found":
                raise RuntimeError("not found")
            return visibility + "\n"
        if args[:2] == ("pr", "create"):
            return pr_url
        return ""

    return gh


def test_an_existing_public_copy_stops_everything(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, _remote, sid, result = _setup(tmp_path, repo, snap)
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match="not private"):
        publish.publish(snap, result, gh=_gh(calls, visibility="PUBLIC"), remote=str(tmp_path / "copy2.git"))


def test_copy_name_is_private_repo_named_after_the_source() -> None:
    assert publish.copy_name("git@github.com:acme/shop.git", "yorrick") == "yorrick/shop-jev-replays"
    assert publish.copy_name("https://github.com/acme/shop", "yorrick") == "yorrick/shop-jev-replays"


def test_publish_pushes_both_results_and_opens_the_compare_pr(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    calls: list[tuple[str, ...]] = []
    url = publish.publish(
        snap, result, gh=_gh(calls, pr_url="https://github.com/yorrick/shop-jev-replays/pull/1\n"), remote=str(remote)
    )
    assert url == "https://github.com/yorrick/shop-jev-replays/pull/1"
    assert (
        "repo",
        "create",
        "yorrick/shop-jev-replays",
        "--private",
        "--description",
        "jev-router fork-check replays",
    ) in calls
    shown = git(remote, "show", f"replay/{sid}/compare:RESULT.txt")
    assert shown == "delegate\n"
    assert git(remote, "rev-parse", f"replay/{sid}/compare^") == git(remote, "rev-parse", f"replay/{sid}/keep")
    for side in ("keep", "delegate", "compare"):
        assert ".env" not in git(remote, "ls-tree", "-r", "--name-only", f"replay/{sid}/{side}").split()
    assert (keep / "RESULT.txt").read_text() == "keep\n"  # the clone's files are untouched
    pr = next(c for c in calls if c[:2] == ("pr", "create"))
    assert pr[pr.index("--base") + 1] == f"replay/{sid}/keep"
    assert pr[pr.index("--head") + 1] == f"replay/{sid}/compare"
    body = pr[pr.index("--body") + 1]
    assert chr(0x2014) not in body
    # Ruling F4: the job's message (it can hold a credential) is never published;
    # the body names the snapshot and where its message is kept locally.
    assert "build it" not in body
    assert sid in body and str(snap / "message.txt") in body


# Ruling T12a: a restored ignored file must never be published, even if the
# replayed session (running with bypass permissions) rewrote the clone's own
# ignore rules or force-added the file.
def test_a_restored_ignored_file_is_refused_even_if_the_clone_stops_ignoring_it(
    tmp_path: Path, repo: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    assert (keep / ".env").exists()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # mask the user's own global excludes (it may list .env)
    (keep / ".gitignore").write_text("node_modules/\n.venv/\n__pycache__/\n")  # the replay dropped the .env line
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"\.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""  # nothing was pushed


def test_a_restored_ignored_file_committed_earlier_in_the_replay_is_also_refused(
    tmp_path: Path, repo: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caught by the history check even once it is no longer at HEAD."""
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    (keep / ".gitignore").write_text("node_modules/\n.venv/\n__pycache__/\n")
    git(keep, "add", "-A")
    git(keep, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "the replay committed .env")
    git(keep, "rm", "-q", ".env")
    git(keep, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "the replay removed it again")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"\.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""


def test_non_ascii_ignored_name_is_refused_even_after_the_clone_stops_ignoring_it(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ls-tree`/`log` quote a non-ASCII path unless run with `-z`; a check that
    reads them without it would never recognize the quoted form as a match."""
    (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + "clé.env\n")
    (repo / "clé.env").write_text("SECRET=x\n")
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    keep = replay.restore(snap, tmp_path / "k")
    delegate = replay.restore(snap, tmp_path / "d")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # mask the user's own global excludes
    (keep / ".gitignore").write_text("node_modules/\n.venv/\n.env\n__pycache__/\n")  # the replay dropped the line
    result = {
        "id": sid,
        "sides": {
            "keep": {"clone": str(keep), "cost": 1.0, "wall_seconds": 60, "calls": 10, "delegated": False},
            "delegate": {"clone": str(delegate), "cost": 0.5, "wall_seconds": 50, "calls": 12, "delegated": True},
        },
    }
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match="clé.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""


# Ruling F5: a restored ignored file's content under another name is refused too.
def test_a_copy_of_a_restored_env_under_another_name_is_refused(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    shutil.copy(keep / ".env", keep / "config.txt")  # the replay copied the secret where git does not ignore it
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"config\.txt in the keep clone holds the content of .* \.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""  # nothing was pushed
    assert not any(c[:2] == ("pr", "create") for c in calls)


def test_a_restored_env_moved_to_another_name_is_refused(tmp_path: Path, repo: Path, snap: Path) -> None:
    """Ruling F10: `mv .env config.txt` leaves no `.env` to hash at check time;
    restore recorded its content before the replay ran."""
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    (keep / ".env").rename(keep / "config.txt")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"config\.txt in the keep clone holds the content of .* \.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""  # nothing was pushed
    assert not any(c[:2] == ("pr", "create") for c in calls)


def test_a_restored_env_edited_then_copied_is_refused(tmp_path: Path, repo: Path, snap: Path) -> None:
    """N1: the copy matches neither id restore recorded, only the `.env` as it is now."""
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    with (keep / ".env").open("a") as env:
        env.write("EXTRA=1\n")
    shutil.copy(keep / ".env", keep / "config.txt")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"config\.txt in the keep clone holds the content of .* \.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""
    assert not any(c[:2] == ("pr", "create") for c in calls)


def test_start_content_only_in_an_untracked_file_is_not_exempt(tmp_path: Path, repo: Path) -> None:
    """F13: only what the committed base (the snapshot HEAD) already holds is
    exempt. An untracked start file with the `.env` content is not: the replay
    deletes it and copies `.env` to `config.txt`, which is refused."""
    (repo / "copy.txt").write_text("TOKEN=x\n")  # untracked, the same bytes as .env
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    (keep / "copy.txt").unlink()
    shutil.copy(keep / ".env", keep / "config.txt")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    sides = {"cost": 1.0, "wall_seconds": 60, "calls": 10, "delegated": True}
    result = {
        "id": sid,
        "sides": {"keep": {**sides, "clone": str(keep)}, "delegate": {**sides, "clone": str(delegate)}},
    }
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"config\.txt in the keep clone holds the content of .* \.env"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""


def test_restore_records_the_restored_files_content(tmp_path: Path, repo: Path, snap: Path) -> None:
    """Same selection as the content check: the 8-byte `.env` is recorded, the
    `node_modules` lock file (a dependency folder, and 2 bytes) is not."""
    replay.restore(snap, tmp_path / "k")
    blobs = json.loads((tmp_path / "k" / "restore.json").read_text())["ignored_blobs"]
    assert blobs == {git(repo, "hash-object", ".env").strip(): ".env"}


def test_a_restore_record_without_its_blob_ids_is_refused(tmp_path: Path) -> None:
    clone = tmp_path / "somewhere" / "repo"
    clone.mkdir(parents=True)
    (clone.parent / "restore.json").write_text(json.dumps({"skipped": [], "ignored": [".env"]}))
    with pytest.raises(RuntimeError, match="ignored_blobs"):
        replay.restored_blobs(clone)


def test_a_copy_committed_then_removed_is_still_refused(tmp_path: Path, repo: Path, snap: Path) -> None:
    """Found among the objects the replay's own commits introduced."""
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    shutil.copy(delegate / ".env", delegate / "settings.ini")
    git(delegate, "add", "settings.ini")
    git(delegate, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "the replay committed a copy")
    git(delegate, "rm", "-q", "settings.ini")
    git(delegate, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "and removed it again")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match=r"settings\.ini in the delegate clone"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert git(remote, "branch", "-a").strip() == ""


def test_an_ignored_file_matching_content_already_in_the_start_state_is_not_refused(tmp_path: Path, repo: Path) -> None:
    """`cp .env.example .env`: the tracked example already holds that content,
    so publishing it leaks nothing new and must not refuse every job."""
    (repo / ".env.example").write_text("TOKEN=x\n")  # the same bytes as the fixture's .env
    git(repo, "add", ".env.example")
    git(repo, "commit", "-qm", "an example env")
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    transcript = write(tmp_path / "t.jsonl", [typed("start"), assistant("r1")])
    sid = snapshot.take_snapshot(tmp_path / "fc", payload(repo, transcript), "build it", "N", EVENT)
    snap = tmp_path / "fc" / "snapshots" / sid
    keep, delegate = replay.restore(snap, tmp_path / "k"), replay.restore(snap, tmp_path / "d")
    remote = tmp_path / "copy.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    sides = {"cost": 1.0, "wall_seconds": 60, "calls": 10, "delegated": True}
    result = {
        "id": sid,
        "sides": {"keep": {**sides, "clone": str(keep)}, "delegate": {**sides, "clone": str(delegate)}},
    }
    calls: list[tuple[str, ...]] = []
    assert publish.publish(snap, result, gh=_gh(calls), remote=str(remote)) == "https://x/pull/1"


# Final Minor 7: the clone's own git hooks never run during publish.
def test_the_clones_git_hooks_never_run(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    ran = tmp_path / "hooks-ran.txt"
    for clone in (keep, delegate):
        for hook in ("pre-commit", "commit-msg", "post-commit", "pre-push", "reference-transaction"):
            path = clone / ".git" / "hooks" / hook
            path.write_text(f"#!/bin/sh\necho {hook} >> {ran}\nexit 1\n")
            path.chmod(0o755)
    calls: list[tuple[str, ...]] = []
    assert publish.publish(snap, result, gh=_gh(calls), remote=str(remote)) == "https://x/pull/1"
    assert not ran.exists()
    assert git(remote, "show", f"replay/{sid}/compare:RESULT.txt") == "delegate\n"


def test_restored_ignored_raises_when_restore_json_is_missing(tmp_path: Path) -> None:
    clone = tmp_path / "somewhere" / "repo"
    clone.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="restore.json"):
        replay.restored_ignored(clone)


def test_restored_ignored_raises_when_the_ignored_key_is_missing(tmp_path: Path) -> None:
    clone = tmp_path / "somewhere" / "repo"
    clone.mkdir(parents=True)
    (clone.parent / "restore.json").write_text(json.dumps({"skipped": []}))
    with pytest.raises(RuntimeError, match="ignored"):
        replay.restored_ignored(clone)


# Ruling T12b: an inconclusive result, or a side missing a priced cost, must
# refuse before touching gh at all.
def test_inconclusive_result_is_refused_with_no_gh_call(tmp_path: Path, repo: Path, snap: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:acme/shop.git")
    calls: list[tuple[str, ...]] = []
    result = {"id": "x", "order": [], "sides": {}, "inconclusive": True, "reason": "no model recorded"}
    with pytest.raises(RuntimeError, match="inconclusive"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(tmp_path / "copy.git"))
    assert calls == []


def test_a_side_missing_cost_is_refused_with_no_gh_call(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    del result["sides"]["delegate"]["cost"]
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match="cost"):
        publish.publish(snap, result, gh=_gh(calls), remote=str(remote))
    assert calls == []


# Ruling T12c: no retry without clearing the copy's branches first, gh login is
# validated, and cmd_publish never lets an exception escape as a traceback.
def test_cmd_publish_refuses_without_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    sid = "20261001-100000-abc"
    _bare_snapshot(tmp_path, sid)
    (tmp_path / "results" / sid).mkdir(parents=True)
    (tmp_path / "results" / sid / "result.json").write_text("{}")
    assert fork_check.main(["publish", sid]) == 1
    assert "Judge" in capsys.readouterr().out


def _bare_snapshot(root: Path, sid: str) -> None:
    """Just enough of a snapshot folder for the runner to accept `sid`."""
    (root / "snapshots" / sid).mkdir(parents=True)
    (root / "snapshots" / sid / "meta.json").write_text("{}")


def test_internal_visibility_is_also_refused(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError, match="not private"):
        publish.publish(snap, result, gh=_gh(calls, visibility="INTERNAL"), remote=str(remote))


def test_an_existing_private_copy_is_not_recreated(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    calls: list[tuple[str, ...]] = []
    publish.publish(snap, result, gh=_gh(calls, visibility="PRIVATE"), remote=str(remote))
    assert not any(c[:2] == ("repo", "create") for c in calls)


def test_existing_remote_branches_refuse_a_retry_instead_of_a_force_push(
    tmp_path: Path, repo: Path, snap: Path
) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    # A branch left over from a previous, failed publish attempt.
    git(keep, "push", "-q", str(remote), f"HEAD:refs/heads/replay/{sid}/keep")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(
        RuntimeError, match=f"gh api -X DELETE repos/yorrick/shop-jev-replays/git/refs/heads/replay/{sid}/keep"
    ) as excinfo:
        publish.publish(snap, result, gh=_gh(calls, visibility="PRIVATE"), remote=str(remote))
    assert "delegate" not in str(excinfo.value) and "compare" not in str(excinfo.value)
    assert not any(c[:2] == ("pr", "create") for c in calls)


def test_existing_remote_branches_list_one_delete_command_per_ref(tmp_path: Path, repo: Path, snap: Path) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    # Two branches left over from a previous, failed publish attempt.
    git(keep, "push", "-q", str(remote), f"HEAD:refs/heads/replay/{sid}/keep")
    git(delegate, "push", "-q", str(remote), f"HEAD:refs/heads/replay/{sid}/delegate")
    calls: list[tuple[str, ...]] = []
    with pytest.raises(RuntimeError) as excinfo:
        publish.publish(snap, result, gh=_gh(calls, visibility="PRIVATE"), remote=str(remote))
    message = str(excinfo.value)
    assert f"gh api -X DELETE repos/yorrick/shop-jev-replays/git/refs/heads/replay/{sid}/keep" in message
    assert f"gh api -X DELETE repos/yorrick/shop-jev-replays/git/refs/heads/replay/{sid}/delegate" in message
    assert "compare" not in message


def test_cmd_publish_records_publish_failed_instead_of_raising(
    tmp_path: Path, repo: Path, snap: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep, delegate, remote, sid, result = _setup(tmp_path, repo, snap)
    fc_root = snap.parent.parent  # `snap` already lives under <fc_root>/snapshots/<sid>
    monkeypatch.setattr(fork_check, "root", lambda: fc_root)
    results_dir = fc_root / "results" / sid
    results_dir.mkdir(parents=True)
    (results_dir / "verdict.json").write_text("{}")
    (results_dir / "result.json").write_text(json.dumps(result))

    def gh(*args: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(publish, "run_gh", gh)
    assert fork_check.main(["publish", sid]) == 1
    assert fork_check.statuses()[sid]["status"] == "publish_failed"


def test_cmd_publish_leaves_the_status_alone_on_an_inconclusive_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fork_check, "root", lambda: tmp_path)
    sid = "20261001-100000-abc"
    _bare_snapshot(tmp_path, sid)
    fork_check.set_status(sid, "inconclusive", "no model recorded")
    results_dir = tmp_path / "results" / sid
    results_dir.mkdir(parents=True)
    (results_dir / "verdict.json").write_text("{}")
    (results_dir / "result.json").write_text(
        json.dumps({"id": sid, "order": [], "sides": {}, "inconclusive": True, "reason": "no model recorded"})
    )
    assert fork_check.main(["publish", sid]) == 1
    assert fork_check.statuses()[sid]["status"] == "inconclusive"  # left as replay set it, not overwritten
    assert "inconclusive" in capsys.readouterr().out
