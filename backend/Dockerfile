# syntax=docker/dockerfile:1.7
# Build context: репо-корень (см. .github/workflows/deploy.yml: context: .)

FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /uvx /bin/

ENV UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Сначала зависимости (без проекта) — слой кэшируется пока не меняется uv.lock/pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Теперь — сам проект.
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked


FROM python:3.12-slim-trixie

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app

# api / worker — оба запускаются через python -m app
EXPOSE 8000
CMD ["python", "-m", "app", "api"]
