# pharmabridge/gateway/Dockerfile
# ────────────────────────────────
# Multi-stage build keeps the final image lean (~180 MB vs ~600 MB).
# Stage 1 (builder): installs all C-extension deps (hiredis, orjson, etc.)
# Stage 2 (runtime): copies only installed packages + app source.

# ── Stage 1: builder ─────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# System build deps for hiredis / orjson C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY gateway/requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security (Kubernetes best practice)
RUN groupadd -r pharma && useradd -r -g pharma -d /app -s /sbin/nologin pharma

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY gateway/ /app/gateway/
COPY db/ /app/db/
WORKDIR /app/gateway

# Kubernetes mounts the frontend ConfigMap / PVC here at runtime
RUN mkdir -p /app/frontend && chown -R pharma:pharma /app

USER pharma

# Expose the port uvicorn will bind to
EXPOSE 8000

# Health check so Docker / K8s can detect crash loops before Prometheus does
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')"

# Use exec form to get proper signal handling (SIGTERM → graceful shutdown)
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--loop", "uvloop", \
     "--log-level", "info", \
     "--access-log"]
