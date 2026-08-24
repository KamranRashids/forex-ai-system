# syntax=docker/dockerfile:1
# ============================================================================
# Forex AI System — backend image (multi-stage, non-root in prod)
# Targets: base -> builder -> prod | dev
# ============================================================================

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# --- builder: resolve dependencies ------------------------------------------
FROM base AS builder
COPY pyproject.toml ./
RUN pip install --prefix=/install .

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
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install .
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]
