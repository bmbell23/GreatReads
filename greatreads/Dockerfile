# GreatReads Dockerfile
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Install system dependencies (tzdata for correct timezone support)
RUN apt-get update && apt-get install -y \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Ensure the TZ env var is honoured inside the container
ENV TZ=America/Denver

# Create app directory
WORKDIR /app

# Copy Python requirements and source code
COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts

# Install the package and dependencies
RUN pip install --no-cache-dir -e .

# Fix bcrypt compatibility issues
RUN pip install --no-cache-dir --upgrade bcrypt==4.0.1

# Ereader backend assets absorbed from :8091 (#22) — summaries + version file.
# Copies of backend/summaries + repo-root version.txt; re-stage into
# ereader-assets/ before build if the originals change. Pointed here via
# EREADER_SUMMARIES_DIR / EREADER_VERSION_FILE in docker-compose.ereader.yml.
# Placed after pip install so re-staging assets doesn't bust the deps layer.
COPY ereader-assets/summaries /app/ereader/summaries
COPY ereader-assets/version.txt /app/ereader/version.txt

# Stamp the image's build time so the web UI can show "last built" automatically
# (#101) — no hand-bumped version, no re-staging of any copied file. This RUN
# sits after every COPY, so Docker's linear layer cache re-runs it on any rebuild
# that ships new code/assets (a true no-op rebuild keeps the prior stamp, which
# is correct — the image didn't actually change). Read live via /api/build-time.
#
# BUILD_STAMP + BUILD_DIRTY (#180) come from scripts/rebuild-ereader.sh as
# build-args: the host has git (this image doesn't), so "was this built from
# uncommitted changes?" (BUILD_DIRTY='M') is computed host-side and baked in.
# A raw `docker compose build` (no helper) falls back to the container clock and
# an empty dirty flag.
ARG BUILD_STAMP=""
ARG BUILD_DIRTY=""
RUN if [ -n "$BUILD_STAMP" ]; then printf '%s\n' "$BUILD_STAMP" > /app/build-stamp.txt; \
    else date '+%y%m%d-%H:%M' > /app/build-stamp.txt; fi
ENV GREATREADS_BUILD_MODIFIED=${BUILD_DIRTY}

# Create necessary directories
RUN mkdir -p /app/data /app/data/covers

# Create non-root user
RUN useradd -m -u 1000 greatreads && \
    chown -R greatreads:greatreads /app
USER greatreads

# Expose port
EXPOSE 8006

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8006/health || exit 1

# Run the application
CMD ["python", "scripts/server.py"]

