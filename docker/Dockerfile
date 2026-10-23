# Python image with uv pre-installed. See https://docs.astral.sh/uv/guides/integration/docker/#available-images
FROM ghcr.io/astral-sh/uv:python3.13-trixie@sha256:62ff9165c89a46680b0684e29625d73585ce82739d598b19743dba6eb632e33c

LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.34.1/bin/linux/${ARCH}/kubectl" \
    && chmod +x /usr/local/bin/kubectl

COPY pyproject.toml uv.lock README.md LICENSE NOTICE alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --all-groups --all-extras

COPY scripts/fetch_acli.py ./scripts/fetch_acli.py
ARG TARGETOS
ARG TARGETARCH
ARG ACLI_VERSION=latest
ARG ACLI_SHA256=
RUN ACLI_OS="${TARGETOS:-linux}" ACLI_ARCH="${TARGETARCH}" ACLI_VERSION="${ACLI_VERSION}" ACLI_SHA256="${ACLI_SHA256}" python scripts/fetch_acli.py --dest vendor/bin/acli

ENV JIRA_ACLI_PATH=/app/vendor/bin/acli

COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts
COPY observability ./observability
COPY openhands ./openhands

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.orchestration.worker"]
