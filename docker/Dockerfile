FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=rebar pyproject.toml hatch_build.py README.md LICENSE /opt/rebar/
COPY --from=rebar src /opt/rebar/src

COPY pyproject.toml README.md ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts

RUN pip install --upgrade pip \
    && pip install -e "/opt/rebar[agents]" "pydantic-ai-slim[openai]>=1,<2" \
    && pip install -e ".[reasoning]"

ARG TARGETOS
ARG TARGETARCH
ARG ACLI_VERSION=latest
ARG ACLI_SHA256=
RUN ACLI_OS="${TARGETOS:-linux}" ACLI_ARCH="${TARGETARCH}" ACLI_VERSION="${ACLI_VERSION}" ACLI_SHA256="${ACLI_SHA256}" python scripts/fetch_acli.py --dest vendor/bin/acli

ENV JIRA_ACLI_PATH=/app/vendor/bin/acli

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.orchestration.worker"]
