"""repo-mcp: read-only cross-repository git research MCP server.

Serves git tools (list / tree / glob / grep / read / read-base64 / log / diff) over
a cache of repositories cloned from a git host, refreshed by a background fetch
loop and by the `repo_refresh` tool. Repos are identified as ``owner/name``.

The only git writes are clone and fetch into the cache, and the git token must be
read-only. Scope is one of two modes. With REPO_ALLOWLIST set, only the named
``owner/name`` repos are cloned, fetched and readable; no owner listing is made, and
repos already in the cache volume but not named are hidden. Otherwise every repo under
GIT_OWNERS is cloned. REPO_DENYLIST excludes repos in either mode and wins over the
allowlist.
"""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

# -- Config -----------------------------------------------------------
def _require_env(name: str, hint: str) -> str:
    """Return a required env var, or raise naming it and what it wants.

    Deployment-specific values have no default.
    """
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"{name} is required and unset - {hint}")
    return val


CACHE_DIR = Path(os.getenv("REPO_CACHE_DIR", "/cache"))
GIT_BASE_URL = _require_env(
    "GIT_BASE_URL", "the base URL of the git host to clone from, e.g. https://git.example.com"
).rstrip("/")


def _parse_allowlist(raw: str) -> tuple[str, ...]:
    """Parse REPO_ALLOWLIST into unique ``owner/name`` entries, in order.

    Raises naming the variable and the bad entry. Duplicates that differ only in case
    collapse, since the forge treats them as one repo. A value with content but no
    entries (e.g. ",") is an error: treating it as unset would drop the scoping the
    operator asked for.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for e in (x.strip() for x in raw.split(",")):
        if not e:
            continue
        parts = e.split("/")
        if len(parts) != 2 or not all(parts) or ".." in parts:
            raise RuntimeError(f"REPO_ALLOWLIST entry '{e}' is invalid - expected 'owner/name'")
        if e.casefold() not in seen:
            seen.add(e.casefold())
            entries.append(e)
    if raw.strip() and not entries:
        raise RuntimeError(f"REPO_ALLOWLIST '{raw.strip()}' has no entries - expected 'owner/name,...'")
    return tuple(entries)


REPO_ALLOWLIST = _parse_allowlist(os.getenv("REPO_ALLOWLIST", ""))
if REPO_ALLOWLIST:
    # One scoping source only, so it is never ambiguous which one applies.
    if os.getenv("GIT_OWNERS", "").strip():
        raise RuntimeError(
            "REPO_ALLOWLIST and GIT_OWNERS are both set - set only one to scope the server"
        )
    GIT_OWNERS: list[str] = []
else:
    GIT_OWNERS = [
        o.strip()
        for o in _require_env(
            "GIT_OWNERS",
            "comma-separated owners/orgs to enumerate, e.g. example-org (or set REPO_ALLOWLIST)",
        ).split(",")
        if o.strip()
    ]
_denylist_raw = os.getenv("REPO_DENYLIST", "").strip()
REPO_DENYLIST = frozenset(r.strip() for r in _denylist_raw.split(",") if r.strip())
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL_SECONDS", "900"))
CLONE_CONCURRENCY = int(os.getenv("CLONE_CONCURRENCY", "4"))
MAX_GREP_MATCHES = int(os.getenv("MAX_GREP_MATCHES", "200"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", "200000"))
MAX_TREE_ENTRIES = int(os.getenv("MAX_TREE_ENTRIES", "2000"))
MAX_DIFF_BYTES = int(os.getenv("MAX_DIFF_BYTES", "120000"))
GIT_TIMEOUT = int(os.getenv("GIT_TIMEOUT_SECONDS", "300"))


def _token() -> str:
    p = Path(os.getenv("GIT_TOKEN_FILE", "/run/secrets/git-token"))
    if p.exists():
        return p.read_text().strip()
    return os.getenv("GIT_TOKEN", "").strip()


# -- Error results ----------------------------------------------------


class ErrorText(str):
    """A tool result that reports a failure. call_tool sends it with isError set,
    so a client can tell it from output (a file or a grep match can itself
    start with "Error:")."""


# -- git + cache plumbing ---------------------------------------------
_locks: dict[str, asyncio.Lock] = {}


def _lock(repo: str) -> asyncio.Lock:
    return _locks.setdefault(repo, asyncio.Lock())


def _repo_dir(repo: str) -> Path:
    return CACHE_DIR / repo  # repo == "owner/name"


def _is_denied(repo: str) -> bool:
    _owner, _, name = repo.partition("/")
    # Case-folded: the forge treats owner/name case-insensitively, so an exact match
    # would let a differently-cased entry through.
    return any(d.casefold() in (repo.casefold(), name.casefold()) for d in REPO_DENYLIST)


def _in_scope(repo: str) -> bool:
    """True if the cache may expose ``repo``. Without an allowlist the cache is not
    filtered (the denylist then only limits discovery)."""
    if not REPO_ALLOWLIST:
        return True
    allowed = {e.casefold() for e in REPO_ALLOWLIST}
    return repo.casefold() in allowed and not _is_denied(repo)


def _validate_repo(repo: str) -> Path:
    """Resolve ``owner/name`` to a cached repo dir; reject traversal / unknowns."""
    if not repo or repo.count("/") != 1 or ".." in repo.split("/"):
        raise ValueError(f"invalid repo id '{repo}' (expected 'owner/name')")
    d = _repo_dir(repo)
    # Out-of-scope repos get the not-in-cache message so the server does not confirm
    # what else exists on the volume.
    if not _in_scope(repo) or not (d / ".git").is_dir():
        raise ValueError(f"repo '{repo}' is not in the cache (try repo_list)")
    return d


def _cached_repos() -> list[str]:
    if not CACHE_DIR.is_dir():
        return []
    return sorted(
        f"{p.parent.name}/{p.name}"
        for p in CACHE_DIR.glob("*/*")
        if (p / ".git").is_dir() and _in_scope(f"{p.parent.name}/{p.name}")
    )


# The token reaches git through a credential helper that reads it from the
# environment of that one git invocation. It is never part of a URL, so it is
# never written into a cache repo's .git/config.
_CRED_HELPER = '!f() { echo username=oauth2; echo "password=$REPO_MCP_GIT_TOKEN"; }; f'


def _git_env() -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    tok = _token()
    if tok:
        env.update({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": _CRED_HELPER,
            "REPO_MCP_GIT_TOKEN": tok,
        })
    return env


async def _git(cwd: Path | None, *args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_git_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _expected_origin(repo: str) -> str:
    return f"{GIT_BASE_URL}/{repo}.git"


def _origin_is_stale(url: str) -> bool:
    """True if a cached repo's origin does not belong to GIT_BASE_URL.

    Stale means a different host, or credentials embedded in the URL. Only
    http(s) origins are judged; anything else (a local path in a test) is left
    alone.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    return p.hostname != urlparse(GIT_BASE_URL).hostname or "@" in p.netloc


async def _ensure_origin(d: Path, repo: str) -> None:
    """Point a cached repo's origin at GIT_BASE_URL if it is anywhere else."""
    rc, url, _ = await _git(d, "remote", "get-url", "origin")
    url = url.strip()
    if rc != 0 or not _origin_is_stale(url):
        return
    want = _expected_origin(repo)
    rc, _, err = await _git(d, "remote", "set-url", "origin", want)
    if rc == 0:
        print(f"[repo-mcp] {repo}: origin re-pointed to {want}", flush=True)
    else:
        print(f"[repo-mcp] {repo}: could not re-point origin: {err.strip()[:200]}", flush=True)


# -- git discovery + cache lifecycle ----------------------------------
async def _discover_allowlist(cli: httpx.AsyncClient) -> list[dict]:
    """Look up each allowlisted repo by name; never lists an owner, so names of other
    repos are not fetched. A repo the token cannot see is skipped."""
    repos: list[dict] = []
    for entry in REPO_ALLOWLIST:
        if _is_denied(entry):
            continue
        owner, name = entry.split("/")
        r = await cli.get(f"/api/v1/repos/{owner}/{name}")
        # A 3xx is a renamed or transferred repo: httpx does not follow it, and failing
        # the attempt would retry the same stale entry forever and block the rest.
        if r.status_code in (403, 404) or 300 <= r.status_code < 400:
            print(f"[repo-mcp] {entry}: not available at this path ({r.status_code}); skipped", flush=True)
            continue
        r.raise_for_status()
        it = r.json()
        # The forge's canonical name decides the cache path, whatever case the entry has.
        full = it.get("full_name") or entry
        if _is_denied(full):
            continue
        repos.append({"repo": full, "clone_url": it.get("clone_url") or f"{GIT_BASE_URL}/{full}.git"})
    return repos


async def _discover() -> list[dict]:
    """[{repo:'owner/name', clone_url:...}] for the allowlist, or for all owners minus
    the denylist."""
    repos: list[dict] = []
    tok = _token()
    headers = {"Authorization": f"token {tok}"} if tok else {}
    async with httpx.AsyncClient(base_url=GIT_BASE_URL, headers=headers, timeout=30) as cli:
        if REPO_ALLOWLIST:
            return await _discover_allowlist(cli)
        for owner in GIT_OWNERS:
            page = 1
            while True:
                # fall back to orgs on 404.
                r = await cli.get(f"/api/v1/users/{owner}/repos", params={"page": page, "limit": 50})
                if r.status_code == 404:
                    r = await cli.get(f"/api/v1/orgs/{owner}/repos", params={"page": page, "limit": 50})
                r.raise_for_status()
                batch = r.json()
                if not batch:
                    break
                for it in batch:
                    full = it.get("full_name") or f"{owner}/{it['name']}"
                    if _is_denied(full):
                        continue
                    repos.append({
                        "repo": full,
                        "clone_url": it.get("clone_url") or f"{GIT_BASE_URL}/{full}.git",
                    })
                if len(batch) < 50:
                    break
                page += 1
    return repos


async def _clone_missing(info: dict, sem: asyncio.Semaphore) -> None:
    repo, d = info["repo"], _repo_dir(info["repo"])
    async with sem, _lock(repo):
        if (d / ".git").is_dir():
            return
        d.parent.mkdir(parents=True, exist_ok=True)
        rc, _out, err = await _git(None, "clone", info["clone_url"], str(d))
        if rc != 0:
            print(f"[repo-mcp] clone failed {repo}: {err.strip()[:200]}", flush=True)


async def _discover_with_retry(attempts: int = 8, base_delay: float = 2.0) -> "list[dict] | None":
    """Discover with exponential backoff, for transient connection errors while a new
    pod's network settles. Returns None only if every attempt failed.
    """
    last: "Exception | None" = None
    for i in range(attempts):
        try:
            return await _discover()
        except Exception as exc:  # noqa: BLE001 - discovery is best-effort
            last = exc
            if i < attempts - 1:
                delay = min(base_delay * (2 ** i), 30.0)
                print(f"[repo-mcp] discovery attempt {i + 1}/{attempts} failed: "
                      f"{exc!r}; retrying in {delay:.0f}s", flush=True)
                await asyncio.sleep(delay)
    print(f"[repo-mcp] discovery gave up after {attempts} attempts: {last!r}", flush=True)
    return None


async def bootstrap() -> None:
    """Discover in-scope repos and clone any that are missing."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    repos = await _discover_with_retry()
    if repos is None:
        return
    scope = f"allowlist ({len(REPO_ALLOWLIST)} repos)" if REPO_ALLOWLIST else f"owners={GIT_OWNERS}"
    print(f"[repo-mcp] discovered {len(repos)} repos ({scope}); cloning missing...", flush=True)
    sem = asyncio.Semaphore(CLONE_CONCURRENCY)
    await asyncio.gather(*(_clone_missing(i, sem) for i in repos))
    print(f"[repo-mcp] bootstrap done; {len(_cached_repos())} repos in cache", flush=True)


async def _sync_worktree(d: Path, repo: str) -> None:
    """Move the checkout onto what the fetch retrieved.

    `git fetch` updates only refs/remotes, and every read tool serves the working
    tree.
    """
    rc, branch, _ = await _git(d, "rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip()
    if rc != 0 or not branch or branch == "HEAD":
        return  # detached: nothing to track
    rc, _, _ = await _git(d, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    if rc != 0:
        return  # no counterpart on the remote

    rc, _, _ = await _git(d, "merge", "--ff-only", f"origin/{branch}")
    if rc == 0:
        return
    # Diverged, for example after an upstream force-push; the cache mirrors the
    # remote, so reset to it.
    rc, _, err = await _git(d, "reset", "--hard", f"origin/{branch}")
    if rc != 0:
        print(f"[repo-mcp] {repo}: could not sync worktree: {err.strip()[:200]}", flush=True)
    else:
        print(f"[repo-mcp] {repo}: worktree reset to origin/{branch} (diverged)", flush=True)


async def _fetch_one(repo: str) -> tuple[int, str, str]:
    d = _repo_dir(repo)
    async with _lock(repo):
        await _ensure_origin(d, repo)
        rc, out, err = await _git(d, "fetch", "--all", "--prune")
        if rc != 0:
            # Log the failure, so a cache serving stale content is visible.
            print(f"[repo-mcp] fetch failed {repo}: {err.strip()[:200]}", flush=True)
            return rc, out, err
        await _git(d, "remote", "set-head", "origin", "-a")  # keep origin/HEAD current
        # Inside the lock: a reader must not see a half-moved worktree.
        await _sync_worktree(d, repo)
        return rc, out, err


async def refresh_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        try:
            await bootstrap()  # pick up newly-created org repos
            for repo in _cached_repos():
                await _fetch_one(repo)
        except Exception as exc:  # noqa: BLE001
            print(f"[repo-mcp] refresh loop error: {exc!r}", flush=True)


# -- tool handlers (all read-only) ------------------------------------
async def _h_repo_list(filt: str = "") -> str:
    rows: list[str] = []
    for repo in _cached_repos():
        if filt and filt not in repo:
            continue
        d = _repo_dir(repo)
        _, head, _ = await _git(d, "log", "-1", "--format=%h %ci %s")
        _, branch, _ = await _git(d, "rev-parse", "--abbrev-ref", "HEAD")
        rows.append(f"{repo}\t[{branch.strip()}]\t{head.strip()[:100]}")
    if not rows:
        return "no repos in cache" + (f" matching '{filt}'" if filt else "") + " (cache may still be warming)"
    return "\n".join(rows)


async def _h_repo_tree(repo: str, path: str = "", depth: int = 0) -> str:
    d = _validate_repo(repo)
    args = ["ls-tree", "-r", "--name-only", "HEAD"]
    if path:
        args += ["--", path]
    rc, out, err = await _git(d, *args)
    if rc != 0:
        return ErrorText(f"Error: {err.strip()}")
    files = [f for f in out.splitlines() if f]
    if depth and depth > 0:
        prefix = path.rstrip("/") + "/" if path else ""
        files = [f for f in files if (f[len(prefix):] if f.startswith(prefix) else f).count("/") < depth]
    truncated = len(files) > MAX_TREE_ENTRIES
    body = "\n".join(files[:MAX_TREE_ENTRIES]) or "(empty)"
    if truncated:
        body += f"\n... ({len(files) - MAX_TREE_ENTRIES} more entries, truncated)"
    return body


async def _h_repo_glob(repo: str, pattern: str) -> str:
    d = _validate_repo(repo)
    try:
        root = d.resolve()
        matches = sorted(
            rel for p in d.glob(pattern)
            if p.is_file() and not (rel := str(p.resolve().relative_to(root))).startswith(".git/")
        )
    except Exception as exc:  # noqa: BLE001
        return ErrorText(f"Error: bad glob: {exc}")
    truncated = len(matches) > MAX_TREE_ENTRIES
    body = "\n".join(matches[:MAX_TREE_ENTRIES]) or "(no matches)"
    if truncated:
        body += f"\n... ({len(matches) - MAX_TREE_ENTRIES} more, truncated)"
    return body


async def _h_repo_grep(repo: str, pattern: str, path: str = "", ignore_case: bool = False) -> str:
    d = _validate_repo(repo)
    args = ["grep", "-n", "-I"]
    if ignore_case:
        args.append("-i")
    args += ["-e", pattern]
    if path:
        args += ["--", path]
    rc, out, err = await _git(d, *args)
    if rc not in (0, 1):  # 1 = no matches (not an error)
        return ErrorText(f"Error: {err.strip()}")
    lines = out.splitlines()
    if not lines:
        return "(no matches)"
    truncated = len(lines) > MAX_GREP_MATCHES
    body = "\n".join(lines[:MAX_GREP_MATCHES])
    if truncated:
        body += f"\n... ({len(lines) - MAX_GREP_MATCHES} more matches, truncated)"
    return body


def _resolve_file(repo: str, path: str) -> Path:
    """Resolve ``path`` inside a cached repo; reject traversal / .git / non-files."""
    d = _validate_repo(repo)
    root = d.resolve()
    target = (d / path).resolve()
    if root != target and root not in target.parents:
        raise ValueError("path escapes repository root")
    if ".git" in target.relative_to(root).parts:
        raise ValueError("refusing to read inside .git")
    if not target.is_file():
        raise ValueError(f"not a file: {path}")
    return target


async def _h_repo_read_file(repo: str, path: str, start_line: int = 0, end_line: int = 0) -> str:
    try:
        target = _resolve_file(repo, path)
    except ValueError as exc:
        return ErrorText(f"Error: {exc}")
    text = target.read_bytes()[:MAX_FILE_BYTES].decode("utf-8", "replace")
    if start_line or end_line:
        lines = text.splitlines()
        s = max(start_line, 1) - 1
        e = end_line or len(lines)
        text = "\n".join(lines[s:e])
    return text or "(empty)"


async def _h_repo_read_file_base64(repo: str, path: str) -> str:
    """Read any file from a cached repo as base64.

    Over the cap this refuses rather than truncating, because a truncated binary is
    unusable and the caller could not tell.
    """
    try:
        target = _resolve_file(repo, path)
    except ValueError as exc:
        return ErrorText(f"Error: {exc}")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        return ErrorText(f"Error: '{path}' is {size} bytes, over MAX_FILE_BYTES ({MAX_FILE_BYTES})")
    return base64.b64encode(target.read_bytes()).decode("ascii") or "(empty)"


async def _h_repo_log(repo: str, path: str = "", limit: int = 20) -> str:
    d = _validate_repo(repo)
    args = ["log", f"-{max(1, min(limit, 200))}", "--format=%h %ci %an %s"]
    if path:
        args += ["--", path]
    rc, out, err = await _git(d, *args)
    if rc != 0:
        return ErrorText(f"Error: {err.strip()}")
    return out.strip() or "(no history)"


async def _h_repo_diff(repo: str, base: str, head: str, path: str = "") -> str:
    d = _validate_repo(repo)
    spec = f"{base}...{head}"  # three-dot: changes on `head` since its merge-base with `base`
    sargs, pargs = ["diff", "--stat", spec], ["diff", spec]
    if path:
        sargs += ["--", path]
        pargs += ["--", path]
    rc1, stat, e1 = await _git(d, *sargs)
    if rc1 != 0:
        return ErrorText(f"Error: {e1.strip()} (did you repo_refresh to fetch both refs?)")
    rc2, patch, _e2 = await _git(d, *pargs)
    if rc2 != 0:
        return ErrorText(f"Error: {_e2.strip()}")
    if len(patch) > MAX_DIFF_BYTES:
        patch = patch[:MAX_DIFF_BYTES] + "\n... (diff truncated)"
    return f"=== diffstat {spec} ===\n{stat.strip()}\n\n=== patch ===\n{patch}"


async def _h_repo_refresh(repo: str = "") -> str:
    if repo:
        # Targeted: fetch one repo, without discovery.
        _validate_repo(repo)
        repos = [repo]
    else:
        # Full reconcile: discover and clone new repos, then fetch all.
        await bootstrap()
        repos = _cached_repos()
    if not repos:
        return "no repos in cache (discovery may be failing - check the git token/connectivity)"
    out: list[str] = []
    for r in repos:
        rc, _o, err = await _fetch_one(r)
        _, head, _ = await _git(_repo_dir(r), "log", "-1", "--format=%h %ci %s")
        status = "ok" if rc == 0 else f"FETCH FAILED: {err.strip()[:120]}"
        out.append(f"{r}: {status} | HEAD {head.strip()[:80]}")
    header = "" if repo else f"reconciled {len(repos)} repos (discovery + fetch)\n"
    return header + "\n".join(out)


# -- MCP server wiring ------------------------------------------------
def release_version() -> str:
    """The release this build reports to clients: REPO_MCP_VERSION,
    stamped into the image from the repo-mcp-v* tag at build time, else `dev`."""
    return os.environ.get("REPO_MCP_VERSION", "").strip() or "dev"


_server = Server("repo-mcp", version=release_version())

_TOOLS: list[Tool] = [
    Tool(name="repo_list",
         description="List the repositories available to read, as owner/name, with their current branch and last commit. Start here: every other repo tool takes one of these ids. The repositories are local mirrors, already cloned and kept fresh, so reading them needs no clone and no network - if one you expect is missing, call repo_refresh rather than looking for it elsewhere.",
         inputSchema={"type": "object", "properties": {
             "filter": {"type": "string", "description": "Only repos whose owner/name contains this", "default": ""}}}),
    Tool(name="repo_tree",
         description="List files in one of the cached repositories (recursive). Omit `path` for the whole repo; `depth` limits how deep. Use it to find where something lives before reading it.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "path": {"type": "string", "description": "Subdirectory; omit for repo root", "default": ""},
             "depth": {"type": "integer", "description": "Max directory depth (0 = unlimited)", "default": 0}},
             "required": ["repo"]}),
    Tool(name="repo_glob",
         description="Find files in one of the cached repositories by glob (supports `**`), e.g. `src/**/*.py`. Faster than listing a whole tree when the shape of the name is known.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "pattern": {"type": "string", "description": "Glob pattern relative to repo root"}},
             "required": ["repo", "pattern"]}),
    Tool(name="repo_grep",
         description="Search the tracked files of one of the cached repositories for a pattern, returning line-numbered matches. This is the way to answer 'where is X defined' or 'what references Y' - it reads the mirror directly, so it needs no checkout.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "pattern": {"type": "string", "description": "Regex / text to search for"},
             "path": {"type": "string", "description": "Limit to a subpath", "default": ""},
             "ignore_case": {"type": "boolean", "description": "Case-insensitive", "default": False}},
             "required": ["repo", "pattern"]}),
    Tool(name="repo_read_file",
         description="Read a file from one of the cached repositories, optionally a line range. This is how to read source: the repository is already on disk, so there is nothing to clone or download first.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "path": {"type": "string", "description": "File path relative to repo root"},
             "start_line": {"type": "integer", "description": "First line (1-based)", "default": 0},
             "end_line": {"type": "integer", "description": "Last line (inclusive)", "default": 0}},
             "required": ["repo", "path"]}),
    Tool(name="repo_read_file_base64",
         description="Read a file from one of the cached repositories as base64, including binary ones (images, archives, PDFs) that repo_read_file would return as replacement characters. Returns the base64 payload alone, ready to decode.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "path": {"type": "string", "description": "File path relative to repo root"}},
             "required": ["repo", "path"]}),
    Tool(name="repo_log",
         description="Show recent commit history for one of the cached repositories, optionally for a single path - what changed, when, and by whom.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "path": {"type": "string", "description": "Limit to a path", "default": ""},
             "limit": {"type": "integer", "description": "How many commits", "default": 20}},
             "required": ["repo"]}),
    Tool(name="repo_diff",
         description="Diff between two refs (three-dot: changes on `head` since its merge-base with `base`). Call repo_refresh first if a ref is a freshly-pushed branch.",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name'"},
             "base": {"type": "string", "description": "Base ref, e.g. 'origin/main'"},
             "head": {"type": "string", "description": "Head ref, e.g. 'origin/my-feature'"},
             "path": {"type": "string", "description": "Limit to a path", "default": ""}},
             "required": ["repo", "base", "head"]}),
    Tool(name="repo_refresh",
         description="Refresh the cache. With `repo`: git-fetch just that one repo (fast - use before diffing a just-pushed branch). Without `repo`: full reconcile - discover + clone any NEW org repos, then fetch all. Returns the resulting HEAD(s).",
         inputSchema={"type": "object", "properties": {
             "repo": {"type": "string", "description": "Repo id 'owner/name' to fetch; omit to reconcile the whole cache (pick up new repos + fetch all)", "default": ""}}}),
]


async def list_tools(_ctx: Any, _params: PaginatedRequestParams) -> ListToolsResult:
    return ListToolsResult(tools=_TOOLS)


async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
    try:
        result = await _dispatch(params.name, params.arguments or {})
    except Exception as exc:  # noqa: BLE001 - surface as a tool error, don't crash the server
        result = ErrorText(f"Error: {exc}")
    return CallToolResult(content=[TextContent(type="text", text=result)],
                          is_error=isinstance(result, ErrorText))

# Each handler is registered with its wire method and params model; the runner
# validates a request against the model before calling the handler.
_server.add_request_handler("tools/list", PaginatedRequestParams, list_tools)
_server.add_request_handler("tools/call", CallToolRequestParams, call_tool)


async def _dispatch(name: str, a: dict) -> str:
    if name == "repo_list":
        return await _h_repo_list(a.get("filter", ""))
    if name == "repo_tree":
        return await _h_repo_tree(a["repo"], a.get("path", ""), int(a.get("depth", 0)))
    if name == "repo_glob":
        return await _h_repo_glob(a["repo"], a["pattern"])
    if name == "repo_grep":
        return await _h_repo_grep(a["repo"], a["pattern"], a.get("path", ""), bool(a.get("ignore_case", False)))
    if name == "repo_read_file":
        return await _h_repo_read_file(a["repo"], a["path"], int(a.get("start_line", 0)), int(a.get("end_line", 0)))
    if name == "repo_read_file_base64":
        return await _h_repo_read_file_base64(a["repo"], a["path"])
    if name == "repo_log":
        return await _h_repo_log(a["repo"], a.get("path", ""), int(a.get("limit", 20)))
    if name == "repo_diff":
        return await _h_repo_diff(a["repo"], a["base"], a["head"], a.get("path", ""))
    if name == "repo_refresh":
        return await _h_repo_refresh(a.get("repo", ""))
    return ErrorText(f"Error: unknown tool '{name}'")


# -- Entry point ------------------------------------------------------
async def _bootstrap_then_loop() -> None:
    await bootstrap()
    await refresh_loop()


def build_sse_app():
    """Build the Starlette ASGI app for the SSE transport. A separate function so tests
    can construct it. Startup and shutdown use the ``lifespan`` context manager.
    """
    from contextlib import asynccontextmanager

    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse.connect_sse(request.scope, request.receive, request._send) as (rs, ws):
            await _server.run(rs, ws, _server.create_initialization_options())

    @asynccontextmanager
    async def lifespan(_app):
        # Populate the cache in the background - don't block readiness on the cold
        # clone; repo_list reflects what's cloned so far. Cancelled on shutdown.
        task = asyncio.create_task(_bootstrap_then_loop())
        try:
            yield
        finally:
            task.cancel()

    return Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        import uvicorn

        uvicorn.run(
            build_sse_app(),
            host=os.getenv("MCP_HOST", "0.0.0.0"),
            port=int(os.getenv("MCP_PORT", "3000")),
        )
    else:
        async def _run_stdio() -> None:
            from mcp.server.stdio import stdio_server
            async with stdio_server() as (rs, ws):
                await _server.run(rs, ws, _server.create_initialization_options())

        asyncio.run(_run_stdio())
