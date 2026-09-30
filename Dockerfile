FROM python:3.12-slim

# System audio and build dependencies for LiveKit, PyAV, and audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    ffmpeg \
    libasound2-dev \
    libportaudio2 \
    portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Copy application source code
COPY backend /app/backend

ENV PYTHONPATH=/app/backend
ENV PYTHONUNBUFFERED=1

# Expose API port
EXPOSE 8000

# Default command runs the LiveKit Voice Agent worker
CMD ["python", "backend/app/main.py", "dev"]
