"""A cached repo whose origin is not on GIT_BASE_URL is re-pointed before it is fetched.

The origin is corrected in place, and the token is never written into the
remote URL.
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
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def cache_repo(tmp_path: Path, monkeypatch) -> Path:
    """A cache checkout at CACHE_DIR/<owner>/<name>, as the server lays them out."""
    monkeypatch.setattr(srv, "CACHE_DIR", tmp_path)
    d = tmp_path / "example-org" / "thing"
    d.mkdir(parents=True)
    git(d, "init", "-q", "-b", "main")
    return d


@pytest.mark.parametrize(
    "url, stale",
    [
        ("https://git.example.com/example-org/thing.git", False),
        ("https://oauth2:tok@git.example.com/example-org/thing.git", True),   # token in URL
        ("https://old-forge.example.com:3000/example-org/thing.git", True),   # other host
        ("https://oauth2:tok@old-forge.example.com:3000/example-org/thing.git", True),
        ("/tmp/somewhere/upstream", False),                                    # not http(s): not ours
        ("git@old-forge.example.com:example-org/thing.git", False),
    ],
)
def test_origin_is_stale_classification(url, stale):
    assert srv._origin_is_stale(url) is stale


def test_stale_origin_is_repointed_in_place(cache_repo, capsys):
    stale = "https://oauth2:oldtoken@old-forge.example.com:3000/example-org/thing.git"
    git(cache_repo, "remote", "add", "origin", stale)

    _run(srv._ensure_origin(cache_repo, "example-org/thing"))

    assert git(cache_repo, "remote", "get-url", "origin") == \
        "https://git.example.com/example-org/thing.git"
    assert "re-pointed" in capsys.readouterr().out


def test_current_origin_is_left_alone(cache_repo, capsys):
    good = "https://git.example.com/example-org/thing.git"
    git(cache_repo, "remote", "add", "origin", good)

    _run(srv._ensure_origin(cache_repo, "example-org/thing"))

    assert git(cache_repo, "remote", "get-url", "origin") == good
    assert "re-pointed" not in capsys.readouterr().out


def test_token_travels_in_env_not_url(monkeypatch):
    monkeypatch.setattr(srv, "_token", lambda: "s3cret")
    env = srv._git_env()
    assert env["REPO_MCP_GIT_TOKEN"] == "s3cret"
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert "s3cret" not in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_no_token_means_no_helper(monkeypatch):
    monkeypatch.setattr(srv, "_token", lambda: "")
    env = srv._git_env()
    assert "GIT_CONFIG_COUNT" not in env
    assert "REPO_MCP_GIT_TOKEN" not in env


def test_failed_fetch_is_logged_and_skips_worktree_sync(cache_repo, capsys, monkeypatch):
    # A remote on the right host that does not exist: fetch fails, nothing else runs.
    git(cache_repo, "remote", "add", "origin",
        "https://git.example.com/example-org/thing.git")
    calls: list[str] = []
    real_git = srv._git

    async def spy(cwd, *args, **kw):
        calls.append(args[0])
        if args[0] == "fetch":
            return 128, "", "fatal: unable to access"
        return await real_git(cwd, *args, **kw)

    monkeypatch.setattr(srv, "_git", spy)
    rc, _, _ = _run(srv._fetch_one("example-org/thing"))

    assert rc == 128
    assert "fetch failed example-org/thing" in capsys.readouterr().out
    assert "fetch" in calls and "merge" not in calls and "remote" in calls  # get-url only
    assert calls.count("remote") == 1
