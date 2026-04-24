FROM python:3.12-slim

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cache layer)
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir fastapi uvicorn pydantic

# Copy source
COPY master/ ./master/
COPY infra/proto/ ./infra/proto/

# Generate gRPC stubs
RUN python -m grpc_tools.protoc \
    -I infra/proto \
    --python_out=master/sync \
    --grpc_python_out=master/sync \
    infra/proto/lucifer_sync.proto || true

# Health check
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000 50051

CMD ["uvicorn", "master.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "4", "--no-access-log"]
