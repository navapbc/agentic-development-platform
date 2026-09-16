FROM python:3.12-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLATFORM_IN_CONTAINER=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.34.1/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

COPY pyproject.toml README.md alembic.ini ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts
COPY observability ./observability
COPY openhands ./openhands

RUN pip install --upgrade pip \
    && pip install -e ".[reasoning,sandbox,review,observability,prompts,storage]" "pydantic-ai-slim[openai]>=1,<2" "ruff>=0.5,<0.13"

ARG TARGETOS
ARG TARGETARCH
ARG ACLI_VERSION=latest
ARG ACLI_SHA256=
RUN ACLI_OS="${TARGETOS:-linux}" ACLI_ARCH="${TARGETARCH}" ACLI_VERSION="${ACLI_VERSION}" ACLI_SHA256="${ACLI_SHA256}" python scripts/fetch_acli.py --dest vendor/bin/acli

ENV JIRA_ACLI_PATH=/app/vendor/bin/acli

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.orchestration.worker"]
