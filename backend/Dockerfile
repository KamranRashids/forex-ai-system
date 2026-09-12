# syntax=docker/dockerfile:1
# ============================================================================
# Forex AI System — backend image (multi-stage, non-root in prod)
# Targets: base -> builder -> prod | dev
# ============================================================================

FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# --- builder: install dependencies from the hashed lock -----------------------
# Reproducible by construction: every dependency is pinned to a hash-verified
# version (requirements.lock, generated from pyproject.toml by pip-compile).
# The project itself is then installed with --no-deps so nothing repulls
# dependencies from PyPI (the wheel is metadata-only here; app/ is copied in
# the runtime stages below).
FROM base AS builder
COPY pyproject.toml requirements.lock ./
RUN pip install --require-hashes --prefix=/install -r requirements.lock \
 && pip install --no-deps --prefix=/install .

# --- prod: minimal runtime, non-root user ------------------------------------
FROM base AS prod
COPY --from=builder /install /usr/local
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
# Migrations are applied on boot; SAFE MODE is asserted by the app itself.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

# --- dev: reload-capable runtime used by docker-compose.yml -------------------
# Source is bind-mounted over /app/app (migrations too); run as root so bind mounts just work.
FROM base AS dev
COPY pyproject.toml requirements.lock ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --require-hashes -r requirements.lock \
 && pip install --no-deps .
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]
