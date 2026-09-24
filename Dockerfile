# One image for both the API and the MCP server (different commands).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.12.7
WORKDIR /app

# dependency layer (cached unless the lock file changes)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY evaluation ./evaluation
RUN uv sync --frozen --no-dev

# persistent state (Chroma index, SQLite records/checkpoints) lives on a volume
ENV CHROMA_DIR=/data/chroma RECORDS_DB=/data/records.sqlite CHECKPOINT_DB=/data/checkpoints.sqlite
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000 8001
CMD ["nfs-agent", "api", "--port", "8000"]
