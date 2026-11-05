# syntax=docker/dockerfile:1

FROM node:25-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.15-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/backend/.venv/bin:$PATH"
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src ./src
RUN uv sync --frozen --no-dev
COPY --from=frontend /app/frontend/dist /app/static

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION \
    STATIC_DIR=/app/static \
    PORT=8080
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn chb.app:app --host 0.0.0.0 --port ${PORT}"]
