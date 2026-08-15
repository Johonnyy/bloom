# Production image. Multi-stage so neither git nor the compiler toolchain ships
# to the server — the builder needs both, the runtime needs neither.
#
# The base is pinned to an exact patch version, never `latest`. The stronger
# form is a digest pin (python:3.11.13-slim-bookworm@sha256:...), which is what
# the ecosystem deploy plan calls for — resolve one with
#   docker buildx imagetools inspect python:3.11.13-slim-bookworm
# and paste it here. It is left as a version tag rather than a made-up digest
# so this file builds as shipped.
FROM python:3.11.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git: agent-mcp-py and agent-runtime are git URLs pinned by commit SHA, not
#   PyPI releases. Without this the build fails at `pip install`, not at runtime.
# build-essential: every dependency ships a wheel today, so this is insurance
#   rather than a requirement — it costs only builder-stage size, and its absence
#   turns a missing wheel on some future base image into a confusing build error.
RUN apt-get update \
    && apt-get install --no-install-recommends -y git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependency metadata first: this layer is cached until pyproject.toml changes,
# so an ordinary source edit does not re-clone the git dependencies.
COPY pyproject.toml ./
COPY app/__init__.py ./app/
RUN pip install .

COPY . .
RUN pip install --no-deps .


FROM python:3.11.13-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl only, for the HEALTHCHECK below. There is no database client to install:
# Bloom's store is a SQLite file and sqlite3 ships in the Python stdlib.
RUN apt-get update \
    && apt-get install --no-install-recommends -y curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv
COPY --chown=appuser:appuser . .

# The SQLite usage log lives here. Mount a volume over it in compose or the log
# is lost on every restart.
RUN mkdir -p /srv/data && chown appuser:appuser /srv/data

USER appuser
EXPOSE 8010

# Shallow probe on purpose: `?deep=true` would make a Postgres blip restart the
# container into the same blip. Readiness gating belongs in compose, not here.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8010/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
