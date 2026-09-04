# Small, reproducible image for Cloud Run / any container platform.
FROM python:3.12-slim

# uv gives us a locked, reproducible install without a full build toolchain.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first: it only changes when the lockfile does.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# Cloud Run injects PORT; the server reads it at start-up.
EXPOSE 8080

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["pet-care-mcp"]
