# Locked runtime. 3.12 matches the interpreter every test is actually run on
# and the one requirements.lock was frozen from — the image used to be 3.11,
# so the code was verified on one interpreter and shipped on another.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl probes /health for the Compose healthcheck. The rest is WeasyPrint's
# native stack plus the fonts the CV and the Arabic RTL report are rendered
# with: the image shipped with NONE of it, so `import weasyprint` inside the
# container died on libgobject. The API alone never rendered a PDF, which is
# the only reason it went unnoticed — anything running the delivery or funnel
# path in a container would have failed at the last step.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
        libcairo2 libgdk-pixbuf-2.0-0 libffi8 shared-mime-info \
        fonts-liberation fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
#
# From the LOCKFILE, not from pyproject: `pip install -e ".[dev]"` resolves
# fresh every build, so a silent upgrade of weasyprint or anthropic or
# sqlalchemy could reach production without a single line of our code
# changing. requirements.lock existed for exactly this and nothing installed
# it. The package itself goes in with --no-deps so pip cannot quietly widen
# a pin behind the lock's back.
COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install -r requirements.lock \
    && pip install -e . --no-deps

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

# Per-tenant object storage lives under /data (mounted volume in Compose).
RUN mkdir -p /data
EXPOSE 8000

CMD ["uvicorn", "career.main:app", "--host", "0.0.0.0", "--port", "8000"]
