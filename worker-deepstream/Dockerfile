# Pin the exact DeepStream SDK tag used in this engagement (P7003). This is
# the NVIDIA-published base image that ships GStreamer, the DeepStream
# plugins (nvstreammux/nvinfer/nvtracker/nvdsanalytics) and the pyds Python
# bindings — none of which are pip-installable, which is why this worker
# can't be a plain python:slim image like worker/ and api/.
FROM nvcr.io/nvidia/deepstream:7.1-triton-multiarch

COPY --from=ghcr.io/astral-sh/uv:0.4.29 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install only the lightweight extras (httpx, redis) into the SDK image's
# system Python — no isolated venv — so the preinstalled `gi`/`pyds`
# bindings stay importable from the same interpreter.
COPY pyproject.toml .
RUN uv pip install --system --no-cache-dir -r pyproject.toml

COPY pipeline.py worker.py ./
COPY config ./config

ENV PYTHONUNBUFFERED=1 \
    GST_DEBUG=2

CMD ["python3", "worker.py"]
