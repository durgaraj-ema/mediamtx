FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.4.29 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev

COPY app ./app
COPY static ./static

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
