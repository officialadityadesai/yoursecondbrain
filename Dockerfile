# Stage 1: Build frontend
FROM node:20-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.11-slim

# System deps:
# - ffmpeg: video chunking and clip trimming
# - libgomp1: required by lancedb (Rust/OpenMP bindings)
# - gcc + build-essential: fallback for any wheels that need compilation
# - curl: used by HEALTHCHECK
# NOTE: libgl1/libglib2.0-0 not needed because we use opencv-python-headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgomp1 \
    gcc \
    build-essential \
    curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (cached layer — only rebuilds when requirements.txt changes)
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r backend/requirements.txt

# Copy application source
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY README.md mainbrainicon.png .env.example ./

# Copy built frontend from Stage 1 into the location FastAPI expects
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist/

# Create volume mount points — Docker overlays these at runtime
RUN mkdir -p brain_data/clips brain_data/notes lancedb_data

# keyring has no D-Bus/SecretService in a headless container.
# Null backend prevents crashes — OAuth tokens won't persist between
# container restarts but all other functionality is unaffected.
ENV PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring

# Working dir must be /app/backend so:
# - python main.py finds main.py
# - os.path.dirname(__file__) == /app/backend
# - os.path.dirname(os.path.dirname(__file__)) == /app (repo root)
WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=4 \
    CMD curl -f http://localhost:8000/ || exit 1

CMD ["python", "main.py"]
