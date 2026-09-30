# deploy/backend.Dockerfile — one image for the FastAPI server and the LiveKit voice worker.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl ffmpeg libasound2-dev libportaudio2 portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    SQLITE_FALLBACK_PATH=/data/math_tutor.db
RUN mkdir -p /data

# Pre-download the worker's model files (Silero VAD etc.) so the first session starts fast.
RUN python backend/app/main.py download-files || echo "model prefetch skipped"

EXPOSE 8000
# Default: the API. The worker service overrides the command (see docker-compose.deploy.yml).
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/app/backend"]
