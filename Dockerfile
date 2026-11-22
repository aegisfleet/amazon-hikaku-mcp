FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.6.6 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY amazon_hikaku_mcp ./amazon_hikaku_mcp

RUN uv sync --frozen --no-dev --no-editable \
    && useradd --create-home --shell /sbin/nologin --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

# Cloud Run injects PORT (default 8080); server.py reads it.
CMD ["amazon-hikaku-mcp"]
