FROM python:3.12-slim

# Install Node.js 20 (required for Claude Code CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Set up application
WORKDIR /app

# Install Python dependencies (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (respects .dockerignore)
COPY . .

# Persist DB and projects under /app/data and /projects respectively.
# docker-compose mounts a named volume at /app/data; point aiosqlite there
# so the DB survives container restarts.
RUN mkdir -p /app/data /projects

# Web UI port
EXPOSE 8080

# Extend stream timeout for long-running tasks (4 hours)
ENV CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=14400000

# Write DB to the mounted volume, not the ephemeral container filesystem.
# Four slashes = absolute path: sqlite+aiosqlite:////app/data/taktis.db
ENV TAKTIS_DATABASE_URL=sqlite+aiosqlite:////app/data/taktis.db

CMD ["python3", "run.py"]
