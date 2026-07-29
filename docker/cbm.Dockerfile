# syntax=docker/dockerfile:1

FROM debian:bookworm-slim
ENV CBM_ALLOWED_ROOT=/workspace

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates zlib1g libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 appuser

COPY vendor/bin/codebase-memory-mcp /usr/local/bin/codebase-memory-mcp
RUN chmod +x /usr/local/bin/codebase-memory-mcp

USER appuser
ENTRYPOINT ["/usr/local/bin/codebase-memory-mcp"]
