FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.4.29 /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN uv sync --no-dev

COPY worker.py detector.py ./

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

CMD ["uv", "run", "python", "worker.py"]
