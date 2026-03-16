FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install project dependencies (cached layer — only rebuilds when deps change)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy source
COPY . .

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["worker"]
