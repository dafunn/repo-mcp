"""`_fetch_one` moves the working branch onto what the fetch retrieved; every read
tool serves the working tree.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import server as srv  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def commit(repo: Path, name: str, body: str, message: str) -> str:
    (repo / name).write_text(body)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)
    return git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture
def cached(tmp_path: Path) -> tuple[Path, Path]:
    """An upstream repo and a cache clone of it, as repo-mcp keeps them:
    a non-bare working checkout."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    commit(upstream, "a.txt", "one\n", "first")

    cache = tmp_path / "cache"
    subprocess.run(["git", "clone", "-q", str(upstream), str(cache)], check=True)
    return upstream, cache


def test_a_fetched_push_reaches_the_working_tree(cached):
    upstream, cache = cached
    new = commit(upstream, "a.txt", "two\n", "second")

    git(cache, "fetch", "-q", "--all", "--prune")
    # Fetched, but the checkout has not moved.
    assert git(cache, "rev-parse", "--short", "HEAD") != new
    assert (cache / "a.txt").read_text() == "one\n"

    _run(srv._sync_worktree(cache, "t/r"))

    assert git(cache, "rev-parse", "--short", "HEAD") == new
    assert (cache / "a.txt").read_text() == "two\n"


def test_an_upstream_force_push_is_followed_rather_than_stalling(cached):
    """A force-pushed repo is reset to the remote; `--ff-only` alone would leave it stale."""
    upstream, cache = cached
    git(upstream, "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--amend", "-m", "rewritten")
    rewritten = git(upstream, "rev-parse", "--short", "HEAD")

    git(cache, "fetch", "-q", "--all", "--prune")
    _run(srv._sync_worktree(cache, "t/r"))
    assert git(cache, "rev-parse", "--short", "HEAD") == rewritten


def test_already_current_is_a_no_op(cached):
    _, cache = cached
    before = git(cache, "rev-parse", "HEAD")
    _run(srv._sync_worktree(cache, "t/r"))
    assert git(cache, "rev-parse", "HEAD") == before


def test_a_detached_head_is_left_alone(cached):
    """Nothing to track, and moving it would surprise whoever detached it."""
    upstream, cache = cached
    commit(upstream, "a.txt", "two\n", "second")
    git(cache, "fetch", "-q", "--all", "--prune")
    git(cache, "checkout", "-q", "--detach", "HEAD")
    before = git(cache, "rev-parse", "HEAD")
    _run(srv._sync_worktree(cache, "t/r"))
    assert git(cache, "rev-parse", "HEAD") == before


def test_a_branch_with_no_remote_counterpart_is_left_alone(cached):
    _, cache = cached
    git(cache, "checkout", "-q", "-b", "local-only")
    before = git(cache, "rev-parse", "HEAD")
    _run(srv._sync_worktree(cache, "t/r"))
    assert git(cache, "rev-parse", "HEAD") == before


def test_fetch_one_syncs_as_well_as_fetches(cached, monkeypatch):
    """The refresh loop and repo_refresh both use _fetch_one."""
    upstream, cache = cached
    new = commit(upstream, "a.txt", "three\n", "third")
    monkeypatch.setattr(srv, "_repo_dir", lambda repo: cache)

    rc, _, _ = _run(srv._fetch_one("t/r"))
    assert rc == 0
    assert git(cache, "rev-parse", "--short", "HEAD") == new
