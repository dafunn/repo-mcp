FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.22@sha256:f513a91fc62fe7c17567eee97230dd198e43edb8a9fbecca843714a4358fe1bc /uv /uvx /bin/

# git runs every tool; ca-certificates for https; tini is PID 1 (see ENTRYPOINT).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --frozen --no-install-project

COPY server.py ./

ENV PYTHONUNBUFFERED=1

# tini reaps orphans and forwards signals; python stays the one real process.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/.venv/bin/python", "server.py"]
