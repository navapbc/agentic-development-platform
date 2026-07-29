# syntax=docker/dockerfile:1

FROM golang:1.26-bookworm AS go-tools
ENV GOFLAGS=-mod=mod GOBIN=/out
RUN mkdir -p /out \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt@v0.0.0-20260729135938-2cb19912a407 \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt-index@v0.0.0-20260729135938-2cb19912a407

FROM debian:bookworm-slim AS scip-cli
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && curl -fsSLo scip.tar.gz "https://github.com/scip-code/scip/releases/download/v0.9.0/scip-linux-${TARGETARCH}.tar.gz" \
 && curl -fsSLo scip.sha256 "https://github.com/scip-code/scip/releases/download/v0.9.0/scip-linux-${TARGETARCH}.tar.gz.sha256" \
 && echo "$(cut -d' ' -f1 scip.sha256)  scip.tar.gz" | sha256sum -c - \
 && tar -xzf scip.tar.gz -C /tmp \
 && install -m 0755 /tmp/scip /usr/local/bin/scip

FROM node:24-bookworm-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip

RUN npm install -g @ast-grep/cli@0.45.0 @sourcegraph/scip-python@0.6.6

COPY --from=go-tools /out/zoekt /out/zoekt-index /usr/local/bin/
COPY --from=scip-cli /usr/local/bin/scip /usr/local/bin/scip

WORKDIR /app
COPY pyproject.toml README.md ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts

RUN pip install -e ".[dev]"

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.repository_intelligence.server"]
