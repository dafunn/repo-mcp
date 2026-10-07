"""Tests for REPO_ALLOWLIST scoping and case-folded denylist matching.

Import-time configuration is exercised by reloading `server` under a patched
environment; the module is restored to the default configuration afterwards.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import server as _default_srv  # noqa: E402,F401


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def reload_with(monkeypatch):
    """Reload `server` with the given env (None unsets); restore it afterwards."""
    def _reload(**env):
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        return importlib.reload(sys.modules["server"])

    yield _reload
    monkeypatch.undo()
    importlib.reload(sys.modules["server"])


@pytest.fixture
def allow(reload_with):
    return reload_with(REPO_ALLOWLIST="me/proj,Other/Thing", GIT_OWNERS=None, REPO_DENYLIST=None)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 300:  # httpx raises for an unfollowed redirect too
            raise RuntimeError(f"http {self.status_code}")


def _stub_client(srv, monkeypatch, responses):
    calls: list[str] = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls.append(url)
            return responses.get(url, _Resp(404))

    monkeypatch.setattr(srv.httpx, "AsyncClient", Client)
    return calls


def _make_repo(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)


@pytest.mark.parametrize("bad", ["justname", "a/b/c", "/name", "owner/", "../x", "a/.."])
def test_bad_allowlist_entry_fails_import(reload_with, bad):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST"):
        reload_with(REPO_ALLOWLIST=f"ok/one,{bad}", GIT_OWNERS=None)


def test_allowlist_with_git_owners_fails_import(reload_with):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST.*GIT_OWNERS"):
        reload_with(REPO_ALLOWLIST="me/proj", GIT_OWNERS="example-org")


def test_allowlist_does_not_need_git_owners(allow):
    assert allow.REPO_ALLOWLIST == ("me/proj", "Other/Thing")
    assert allow.GIT_OWNERS == []


@pytest.mark.parametrize("junk", [",", " , ", ",,", ", ,"])
def test_allowlist_with_no_entries_fails_closed(reload_with, junk):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST"):
        reload_with(REPO_ALLOWLIST=junk, GIT_OWNERS="example-org")


def test_whitespace_only_allowlist_is_unset(reload_with):
    srv = reload_with(REPO_ALLOWLIST="   ", GIT_OWNERS="example-org")
    assert srv.REPO_ALLOWLIST == ()
    assert srv.GIT_OWNERS == ["example-org"]
    assert srv._in_scope("anyone/anything")


def test_unset_allowlist_still_requires_git_owners(reload_with):
    with pytest.raises(RuntimeError, match="GIT_OWNERS"):
        reload_with(REPO_ALLOWLIST=None, GIT_OWNERS=None)


def test_discovery_fetches_each_entry_and_never_lists_owner(allow, monkeypatch):
    calls = _stub_client(allow, monkeypatch, {
        "/api/v1/repos/me/proj": _Resp(200, {"full_name": "me/proj", "clone_url": "https://h/me/proj.git"}),
        "/api/v1/repos/Other/Thing": _Resp(200, {"full_name": "Other/Thing"}),
    })
    repos = _run(allow._discover())
    assert calls == ["/api/v1/repos/me/proj", "/api/v1/repos/Other/Thing"]
    assert repos == [
        {"repo": "me/proj", "clone_url": "https://h/me/proj.git"},
        {"repo": "Other/Thing", "clone_url": f"{allow.GIT_BASE_URL}/Other/Thing.git"},
    ]


def test_discovery_skips_404_and_keeps_others(allow, monkeypatch):
    _stub_client(allow, monkeypatch, {
        "/api/v1/repos/me/proj": _Resp(404),
        "/api/v1/repos/Other/Thing": _Resp(200, {"full_name": "Other/Thing"}),
    })
    assert [r["repo"] for r in _run(allow._discover())] == ["Other/Thing"]


def test_discovery_skips_403(allow, monkeypatch):
    _stub_client(allow, monkeypatch, {
        "/api/v1/repos/me/proj": _Resp(403),
        "/api/v1/repos/Other/Thing": _Resp(200, {"full_name": "Other/Thing"}),
    })
    assert [r["repo"] for r in _run(allow._discover())] == ["Other/Thing"]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_discovery_skips_redirect_and_keeps_others(allow, monkeypatch, status):
    _stub_client(allow, monkeypatch, {
        "/api/v1/repos/me/proj": _Resp(status),
        "/api/v1/repos/Other/Thing": _Resp(200, {"full_name": "Other/Thing"}),
    })
    assert [r["repo"] for r in _run(allow._discover())] == ["Other/Thing"]


def test_discovery_other_error_fails_the_attempt(allow, monkeypatch):
    _stub_client(allow, monkeypatch, {"/api/v1/repos/me/proj": _Resp(500)})
    with pytest.raises(RuntimeError):
        _run(allow._discover())


def test_discovery_uses_forge_case_for_clone_path(reload_with, monkeypatch):
    srv = reload_with(REPO_ALLOWLIST="ME/Proj", GIT_OWNERS=None, REPO_DENYLIST=None)
    _stub_client(srv, monkeypatch, {
        "/api/v1/repos/ME/Proj": _Resp(200, {"full_name": "me/proj"}),
    })
    assert [r["repo"] for r in _run(srv._discover())] == ["me/proj"]


def test_case_differing_entry_is_visible_in_cache(reload_with, tmp_path, monkeypatch):
    srv = reload_with(REPO_ALLOWLIST="ME/Proj", GIT_OWNERS=None, REPO_DENYLIST=None)
    monkeypatch.setattr(srv, "CACHE_DIR", tmp_path)
    _make_repo(tmp_path, "me/proj")
    assert srv._cached_repos() == ["me/proj"]
    assert srv._validate_repo("me/proj").name == "proj"


def test_cached_repos_hides_unallowed(allow, tmp_path, monkeypatch):
    monkeypatch.setattr(allow, "CACHE_DIR", tmp_path)
    _make_repo(tmp_path, "me/proj")
    _make_repo(tmp_path, "me/secret")
    assert allow._cached_repos() == ["me/proj"]


def test_validate_rejects_unallowed_with_not_in_cache_message(allow, tmp_path, monkeypatch):
    monkeypatch.setattr(allow, "CACHE_DIR", tmp_path)
    _make_repo(tmp_path, "me/secret")
    with pytest.raises(ValueError) as hidden:
        allow._validate_repo("me/secret")
    with pytest.raises(ValueError) as missing:
        allow._validate_repo("me/absent")
    assert "is not in the cache" in str(hidden.value)
    assert str(hidden.value).replace("me/secret", "X") == str(missing.value).replace("me/absent", "X")


def test_denylist_wins_over_allowlist(reload_with, tmp_path, monkeypatch):
    srv = reload_with(REPO_ALLOWLIST="me/proj,me/other", GIT_OWNERS=None, REPO_DENYLIST="me/proj")
    monkeypatch.setattr(srv, "CACHE_DIR", tmp_path)
    _make_repo(tmp_path, "me/proj")
    _make_repo(tmp_path, "me/other")
    assert srv._cached_repos() == ["me/other"]
    with pytest.raises(ValueError, match="not in the cache"):
        srv._validate_repo("me/proj")
    calls = _stub_client(srv, monkeypatch, {
        "/api/v1/repos/me/other": _Resp(200, {"full_name": "me/other"}),
    })
    assert [r["repo"] for r in _run(srv._discover())] == ["me/other"]
    assert calls == ["/api/v1/repos/me/other"]


@pytest.mark.parametrize("allowlist", [None, "me/proj"])
def test_is_denied_is_case_insensitive(reload_with, allowlist):
    srv = reload_with(
        REPO_ALLOWLIST=allowlist,
        GIT_OWNERS="example-org" if allowlist is None else None,
        REPO_DENYLIST="Example-Org/Secrets,Infra-Config",
    )
    assert srv._is_denied("example-org/secrets")      # full entry, different case
    assert srv._is_denied("team-a/infra-config")      # bare entry, different case
    assert not srv._is_denied("team-a/example-repo")


def test_unset_allowlist_leaves_cache_unfiltered(reload_with, tmp_path, monkeypatch):
    srv = reload_with(REPO_ALLOWLIST=None, GIT_OWNERS="example-org", REPO_DENYLIST=None)
    monkeypatch.setattr(srv, "CACHE_DIR", tmp_path)
    _make_repo(tmp_path, "me/any")
    assert srv._cached_repos() == ["me/any"]
