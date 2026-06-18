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

