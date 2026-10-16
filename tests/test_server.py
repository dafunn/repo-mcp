"""Tests for repo-mcp read-only handlers, against a real temp git repo cache.

No network / git host: we monkeypatch CACHE_DIR to a tmp dir holding a real `owner/name`
git repo and exercise the tools directly. Needs `git` on PATH.
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.types import CallToolRequestParams

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import server as srv  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
    )


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "CACHE_DIR", tmp_path)
    repo = tmp_path / "me" / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "hello.py").write_text("def greet():\n    return 'hi'\n")
    (repo / "README.md").write_text("# proj\n")
    (repo / "src").mkdir()
    (repo / "src" / "core.py").write_text("X = 42  # the answer\n")
    (repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    return tmp_path


def test_cached_repos_and_validate(cache):
    assert srv._cached_repos() == ["me/proj"]
    assert srv._validate_repo("me/proj").name == "proj"


def test_validate_rejects_traversal_and_unknown(cache):
    for bad in ("../etc", "me/nope", "noslashhere", "a/b/c"):
        with pytest.raises(ValueError):
            srv._validate_repo(bad)


def test_repo_list(cache):
    out = _run(srv._h_repo_list())
    assert "me/proj" in out and "initial" in out


def test_repo_tree_and_depth(cache):
    full = _run(srv._h_repo_tree("me/proj"))
    assert "hello.py" in full and "src/core.py" in full
    shallow = _run(srv._h_repo_tree("me/proj", depth=1))
    assert "hello.py" in shallow and "src/core.py" not in shallow


def test_repo_glob(cache):
    out = _run(srv._h_repo_glob("me/proj", "**/*.py"))
    assert "hello.py" in out and "src/core.py" in out
    assert "README.md" not in out


def test_repo_grep(cache):
    out = _run(srv._h_repo_grep("me/proj", "the answer"))
    assert "src/core.py" in out and "42" in out
    assert _run(srv._h_repo_grep("me/proj", "nonexistent_zzz")) == "(no matches)"


def test_repo_read_file_and_range(cache):
    assert "def greet" in _run(srv._h_repo_read_file("me/proj", "hello.py"))
    assert _run(srv._h_repo_read_file("me/proj", "hello.py", 2, 2)).strip() == "return 'hi'"


def test_repo_read_file_path_escape(cache):
    assert "escapes repository root" in _run(srv._h_repo_read_file("me/proj", "../../etc/passwd"))


def test_repo_read_file_refuses_git(cache):
    assert "refusing to read inside .git" in _run(srv._h_repo_read_file("me/proj", ".git/config"))


def test_repo_read_file_base64_roundtrips_binary(cache):
    raw = b"\x89PNG\r\n\x1a\n\x00\xff\xfe binary"
    assert base64.b64decode(_run(srv._h_repo_read_file_base64("me/proj", "logo.png"))) == raw
    # The text reader mangles those bytes - that's what this tool exists for.
    assert chr(0xFFFD) in _run(srv._h_repo_read_file("me/proj", "logo.png"))


def test_repo_read_file_base64_refuses_over_cap(cache, monkeypatch):
    # Refuse, don't truncate: a clipped base64 payload decodes to a corrupt file
    # with nothing to signal it was cut.
    monkeypatch.setattr(srv, "MAX_FILE_BYTES", 4)
    out = _run(srv._h_repo_read_file_base64("me/proj", "logo.png"))
    assert "over MAX_FILE_BYTES" in out


def test_repo_read_file_base64_containment(cache):
    assert "escapes repository root" in _run(srv._h_repo_read_file_base64("me/proj", "../../etc/passwd"))
    assert "refusing to read inside .git" in _run(srv._h_repo_read_file_base64("me/proj", ".git/config"))
    assert "not a file" in _run(srv._h_repo_read_file_base64("me/proj", "src"))


def test_repo_log(cache):
    assert "initial" in _run(srv._h_repo_log("me/proj"))


def test_is_denied(monkeypatch):
    monkeypatch.setattr(srv, "REPO_DENYLIST", frozenset({"infra-config", "example-org/secrets"}))
    assert srv._is_denied("team-a/infra-config")      # bare-name match
    assert srv._is_denied("example-org/secrets")      # full owner/name match
    assert not srv._is_denied("team-a/example-repo")


def test_dispatch_unknown_tool(cache):
    assert "unknown tool" in _run(srv._dispatch("nope", {}))


def test_repo_refresh_no_arg_reconciles(cache, monkeypatch):
    # No-arg refresh runs discovery (bootstrap) to pick up new repos, then fetches.
    called = {"bootstrap": 0}

    async def fake_bootstrap():
        called["bootstrap"] += 1

    async def fake_fetch(r):
        return (0, "", "")

    monkeypatch.setattr(srv, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(srv, "_fetch_one", fake_fetch)
    out = _run(srv._h_repo_refresh())
    assert called["bootstrap"] == 1
    assert "me/proj" in out


def test_repo_refresh_single_repo_skips_discovery(cache, monkeypatch):
    # Targeted single-repo refresh must never trigger an org-wide discovery scan.
    called = {"bootstrap": 0}

    async def fake_bootstrap():
        called["bootstrap"] += 1

    async def fake_fetch(r):
        return (0, "", "")

    monkeypatch.setattr(srv, "bootstrap", fake_bootstrap)
    monkeypatch.setattr(srv, "_fetch_one", fake_fetch)
    out = _run(srv._h_repo_refresh("me/proj"))
    assert called["bootstrap"] == 0
    assert "me/proj" in out


def test_discover_with_retry_recovers(monkeypatch):
    # A transient discovery failure is retried.
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise srv.httpx.ConnectError("All connection attempts failed")
        return [{"repo": "example-org/x", "clone_url": "https://h/example-org/x.git"}]

    monkeypatch.setattr(srv, "_discover", flaky)
    out = _run(srv._discover_with_retry(attempts=5, base_delay=0))
    assert out == [{"repo": "example-org/x", "clone_url": "https://h/example-org/x.git"}]
    assert calls["n"] == 3


def test_discover_with_retry_gives_up(monkeypatch):
    async def always_fail():
        raise srv.httpx.ConnectError("nope")

    monkeypatch.setattr(srv, "_discover", always_fail)
    assert _run(srv._discover_with_retry(attempts=3, base_delay=0)) is None


def test_build_sse_app_constructs():
    # Builds the SSE app, which the handler tests never touch. Skipped when
    # starlette is not installed.
    pytest.importorskip("starlette")
    pytest.importorskip("mcp.server.sse")
    app = srv.build_sse_app()
    from starlette.applications import Starlette
    assert isinstance(app, Starlette)
    assert "/sse" in {getattr(r, "path", None) for r in app.routes}


# -- error results carry isError ---------------------------------------------


def _call(name: str, args: dict):
    return _run(srv.call_tool(None, CallToolRequestParams(name=name, arguments=args)))


@pytest.mark.parametrize("name, args, text", [
    ("repo_tree", {"repo": "me/nope"}, "Error: repo 'me/nope' is not in the cache (try repo_list)"),
    ("repo_grep", {"repo": "../etc", "pattern": "x"},
     "Error: invalid repo id '../etc' (expected 'owner/name')"),
    ("repo_read_file", {"repo": "me/proj", "path": "../../etc/passwd"},
     "Error: path escapes repository root"),
    ("repo_read_file", {"repo": "me/proj", "path": "missing.py"}, "Error: not a file: missing.py"),
    ("repo_diff", {"repo": "me/proj", "base": "nope-a", "head": "nope-b"}, None),
    ("repo_log", {"repo": "me/proj", "path": "", "limit": "x"}, None),
    ("nope", {}, "Error: unknown tool 'nope'"),
])
def test_refusals_and_failures_are_error_results(cache, name, args, text):
    result = _call(name, args)
    assert result.is_error is True
    assert result.content[0].text.startswith("Error: ")
    if text is not None:
        assert result.content[0].text == text


def test_a_file_reading_like_an_error_is_not_an_error(cache):
    repo = cache / "me" / "proj"
    (repo / "notes.txt").write_text("Error: this is just text\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "notes")
    result = _call("repo_read_file", {"repo": "me/proj", "path": "notes.txt"})
    assert not result.is_error
    assert "Error: this is just text" in result.content[0].text


# -- Release version ------------------------------------------------


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("REPO_MCP_VERSION", raising=False)
    assert srv.release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("REPO_MCP_VERSION", "  ")
    assert srv.release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("REPO_MCP_VERSION", "0.2.0-3-gabc1234")
    assert srv.release_version() == "0.2.0-3-gabc1234"


def test_server_reports_the_release_version():
    assert srv._server.version == srv.release_version()
