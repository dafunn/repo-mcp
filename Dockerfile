FROM docker.io/library/python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

# git runs every tool; ca-certificates for https; tini is PID 1 (see ENTRYPOINT).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-install-project

COPY server.py ./

# The release version reported to MCP clients, from the build's `git describe`
# against the repo-mcp-v* tags. A build without it reports `dev`.
# Stamp a release with
#     --build-arg REPO_MCP_VERSION=$(git describe --tags --match 'repo-mcp-v*' | sed 's/^repo-mcp-v//')
ARG REPO_MCP_VERSION=dev
# The RUN uses the arg so the layer cache keys on it; an ENV alone can be
# reused across builds with different values and report a stale version.
RUN printf '%s\n' "${REPO_MCP_VERSION}" > /app/VERSION
ENV REPO_MCP_VERSION=${REPO_MCP_VERSION}

ENV PYTHONUNBUFFERED=1

# tini reaps orphans and forwards signals; python stays the one real process.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/.venv/bin/python", "server.py"]
