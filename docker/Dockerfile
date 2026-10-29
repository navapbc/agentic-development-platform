FROM python:3.12-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLATFORM_IN_CONTAINER=1 \
    BIND_HOST=0.0.0.0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH="$(dpkg --print-architecture)" \
    && case "${ARCH}" in \
         amd64) KUBECTL_SHA256=65691ff77eb6fa44c908b77a1082c9f092c3b9733b5cefabec0d1104890e21a8 ;; \
         arm64) KUBECTL_SHA256=ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7 ;; \
         *) echo "kubectl has no pinned digest for ${ARCH}" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.37.1/bin/linux/${ARCH}/kubectl" \
    && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod +x /usr/local/bin/kubectl

COPY --from=ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md LICENSE NOTICE alembic.ini ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts
COPY observability ./observability
COPY openhands ./openhands

RUN uv sync --locked --extra worker

ARG TARGETOS
ARG TARGETARCH
ARG INSTALL_ACLI=false
ARG ACLI_VERSION
ARG ACLI_SHA256
RUN if [ "${INSTALL_ACLI}" = "true" ]; then \
      ACLI_OS="${TARGETOS:-linux}" ACLI_ARCH="${TARGETARCH}" ACLI_VERSION="${ACLI_VERSION}" ACLI_SHA256="${ACLI_SHA256}" python scripts/fetch_acli.py --dest vendor/bin/acli; \
    fi

ENV JIRA_ACLI_PATH=/app/vendor/bin/acli

ARG AGENT_BUILD_ID=0.3.0
ENV AGENT_BUILD_ID=${AGENT_BUILD_ID}

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.orchestration.worker"]
