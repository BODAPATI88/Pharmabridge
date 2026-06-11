# pharmabridge/workers/Dockerfile
# ─────────────────────────────────
# Playwright requires a full Chromium install – this image is larger (~600 MB).
# Workers that don't use Playwright (netmeds, pharmeasy) could use a slimmer
# variant; for simplicity all workers share this image and toggle via USE_MOCK.

FROM python:3.11-slim AS base

# System deps for Playwright Chromium + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime deps
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libxshmfence1 \
    # Build tools
    gcc libffi-dev \
    # Fonts for rendering
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY workers/requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy gateway models (shared schema) and worker source
# In production: extract models.py to a pb-common package on PyPI
COPY gateway/models.py ./gateway/models.py
COPY workers/worker_base.py workers/worker_1mg.py workers/worker_pharmeasy.py workers/worker_netmeds.py ./

# Non-root user
RUN groupadd -r pharma && useradd -r -g pharma -d /app -s /sbin/nologin pharma \
    && chown -R pharma:pharma /app
USER pharma

# WORKER_SCRIPT is set per-deployment in the K8s pod spec
# e.g. CMD ["python", "worker_1mg.py"]
ENV PYTHONPATH="/app:/app/gateway"

CMD ["python", "worker_1mg.py"]
