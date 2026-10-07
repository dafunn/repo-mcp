# repo-mcp

Read-only cross-repository git research MCP server. Serves git tools over a
local cache of repositories cloned from a git host, refreshed by a background
`git fetch` loop and by the `repo_refresh` tool.

Repos are identified as `owner/name`. The read scope is one of two modes. With
`GIT_OWNERS`, every repo the host lists for those owners is cloned except those in
`REPO_DENYLIST`; a repo created later is in scope until it is denied. With
`REPO_ALLOWLIST`, only the named repos are cloned, fetched and readable: no owner
listing is made, and repos that are not named stay hidden even if the cache volume
holds them. Use the allowlist where only named repos should be reachable.
`REPO_DENYLIST` applies in both modes and wins over the allowlist.

## Tools (all read-only)
- `repo_list(filter?)` - cached repos + current branch + last commit.
- `repo_tree(repo, path?, depth?)` - list files (recursive).
- `repo_glob(repo, pattern)` - find files by glob (supports `**`).
- `repo_grep(repo, pattern, path?, ignore_case?)` - line-numbered matches.
- `repo_read_file(repo, path, start_line?, end_line?)` - read a file (range-able).
- `repo_read_file_base64(repo, path)` - read any file as base64, including binary ones;
  refused (not truncated) over `MAX_FILE_BYTES`.
- `repo_log(repo, path?, limit?)` - commit history.
- `repo_diff(repo, base, head, path?)` - three-dot diff (changes on `head` since its
  merge-base with `base`); `repo_refresh` first for a freshly-pushed branch.
- `repo_refresh(repo?)` - `git fetch --all --prune` one/all repos; returns new HEAD(s).

## Config (env)
| var | default | meaning |
| --- | --- | --- |
| `MCP_TRANSPORT` | `stdio` | set `sse` in-cluster |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `3000` | SSE bind |
| `REPO_CACHE_DIR` | `/cache` | repo cache (a PVC in-cluster) |
| `GIT_BASE_URL` | _(required)_ | git host API + clone base, e.g. `https://git.example.com` |
| `GIT_OWNERS` | _(required unless `REPO_ALLOWLIST` is set)_ | owners/orgs to enumerate, e.g. `example-org`; setting it together with `REPO_ALLOWLIST` is an error |
| `REPO_ALLOWLIST` | _(empty)_ | comma-separated `owner/name` repos to serve, and no others; matched case-insensitively |
| `GIT_TOKEN_FILE` | `/run/secrets/git-token` | read-only token (falls back to `GIT_TOKEN`) |
| `REPO_DENYLIST` | _(empty)_ | `owner/name` or bare `name` to exclude, matched case-insensitively; wins over `REPO_ALLOWLIST` |
| `REFRESH_INTERVAL_SECONDS` | `900` | background fetch cadence |
| `CLONE_CONCURRENCY` | `4` | parallel clones on bootstrap |
| `MAX_GREP_MATCHES` / `MAX_TREE_ENTRIES` / `MAX_FILE_BYTES` / `MAX_DIFF_BYTES` | 200 / 2000 / 200000 / 120000 | output caps |

On startup the cache is populated in the background; `repo_list` shows what has
been cloned so far. Each refresh cycle also discovers and clones new repositories.

## Build / run
```
docker build -t repo-mcp .      # or: podman build -t repo-mcp .

uv sync --frozen               # Python 3.14 and uv
MCP_TRANSPORT=sse REPO_CACHE_DIR=/tmp/cache GIT_TOKEN=... .venv/bin/python server.py
```
Tests: `uv sync --frozen --extra dev && .venv/bin/python -m pytest tests/` (needs `git`).
