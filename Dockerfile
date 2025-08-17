# syntax=docker/dockerfile:1

# Builder: create a wheel with Poetry
FROM python:3.10-slim AS builder

ENV POETRY_VIRTUALENVS_CREATE=false
RUN pip install "poetry==1.8.3"

WORKDIR /build
# Copy project files
COPY pyproject.toml poetry.lock* ./
COPY . .
# Build wheel
RUN poetry build -f wheel

# Runtime: AWS Lambda Python base
FROM public.ecr.aws/lambda/python:3.10

# Install runtime deps (no Poetry needed here)
# Copy built wheel from builder and install it
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Set the Lambda handler
CMD ["shootflow.s3_ingest.handler"]