# syntax=docker/dockerfile:1.7
# Build context: репо-корень (см. .github/workflows/deploy.yml: context: .)
#
# Зависимости ставятся pip'ом прямо из pyproject.toml (uv в проекте не используем,
# uv.lock не нужен). Один источник правды по зависимостям — [project.dependencies].

FROM python:3.12-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# git — для pip-установки ebay-library из коммита (как в parser_ebay)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# api / worker — оба запускаются через python -m app
EXPOSE 8000
CMD ["python", "-m", "app", "api"]
