# syntax=docker/dockerfile:1

ARG TEMPORAL_SERVER_IMAGE=temporalio/server:1.32.1@sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049

FROM golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS healthprobe
WORKDIR /src
COPY docker/healthprobe/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM ${TEMPORAL_SERVER_IMAGE}
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
USER temporal
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=15 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:7243/api/v1/namespaces/temporal-system"]
