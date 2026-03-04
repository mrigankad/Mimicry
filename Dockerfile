# ── Mimicry — Dockerfile ──────────────────────────────────────────────────
# python:3.13-slim + ffmpeg (needed by pydub) + all Python deps
# Model weights (~800 MB) are downloaded at first run into a named volume.
#
# Build:  docker build -t mimicry .
# Run:    docker compose up
# ─────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim

# --- system deps -----------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

# --- Python deps -----------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App source ------------------------------------------------------------
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create storage dirs (will be overridden by bind-mount or named volume)
RUN mkdir -p backend/storage/voices \
             backend/storage/embeddings \
             backend/storage/outputs

# --- Runtime ---------------------------------------------------------------
# HuggingFace model cache lives in /root/.cache → mount as a named volume
# so weights survive container restarts.
ENV HF_HOME=/root/.cache/huggingface
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
