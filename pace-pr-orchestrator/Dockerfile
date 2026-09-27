# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — dependency builder
#   Compiles / downloads all Python packages into a clean prefix so the final
#   image only copies wheels, never a compiler toolchain.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build-time OS deps needed by some packages (e.g. presidio uses regex C-ext).
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt

# Download the spaCy model required by Presidio's NLP engine.
# Run inside the builder so the model sits in the installed prefix.
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    python -m spacy download en_core_web_sm --target /install/lib/python3.11/site-packages


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1b — security-tool downloader
#   Fetches gitleaks and trivy binaries so they are available in the runtime
#   image without requiring curl/wget at container startup.
# ─────────────────────────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS tooling

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# gitleaks — secret scanner
ARG GITLEAKS_VERSION=8.18.4
RUN curl -sSfL \
    "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks \
    && chmod +x /usr/local/bin/gitleaks

# trivy — vulnerability / misconfiguration scanner
ARG TRIVY_VERSION=0.51.4
RUN curl -sSfL \
    "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
    | tar -xz -C /usr/local/bin trivy \
    && chmod +x /usr/local/bin/trivy


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Drop privileges: run as a non-root user.
RUN useradd --create-home --shell /bin/bash appuser

WORKDIR /app

# Copy installed packages (including the spaCy model) from the builder stage.
COPY --from=builder /install /usr/local

# Copy gitleaks and trivy binaries from the tooling stage.
COPY --from=tooling /usr/local/bin/gitleaks /usr/local/bin/gitleaks
COPY --from=tooling /usr/local/bin/trivy    /usr/local/bin/trivy

# Copy application source.
COPY src/ ./src/

# Metadata
LABEL org.opencontainers.image.title="pace-pr-orchestrator" \
    org.opencontainers.image.description="LangGraph-based automated PR code-review service" \
    org.opencontainers.image.version="1.0.0"

USER appuser

# Environment defaults (overridden at runtime via --env or docker-compose).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    PORT=8000

EXPOSE 8000

CMD ["python", "-m", "src.main"]
