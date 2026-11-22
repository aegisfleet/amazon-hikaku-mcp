FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY amazon_hikaku_mcp ./amazon_hikaku_mcp
RUN pip install .

# Cloud Run injects PORT (default 8080); server.py reads it.
CMD ["amazon-hikaku-mcp"]
