# Locked runtime: Python 3.11 (CLAUDE.md). Slim base + the application package.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by the Compose healthcheck to probe /health.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

# Per-tenant object storage lives under /data (mounted volume in Compose).
RUN mkdir -p /data
EXPOSE 8000

CMD ["uvicorn", "career.main:app", "--host", "0.0.0.0", "--port", "8000"]
