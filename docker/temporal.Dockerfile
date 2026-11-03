# syntax=docker/dockerfile:1

ARG TEMPORAL_SERVER_IMAGE=temporalio/server:1.32.1@sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b

FROM golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS healthprobe
WORKDIR /src
COPY docker/healthprobe/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM ${TEMPORAL_SERVER_IMAGE}
ARG RDS_CA_BUNDLE_URL
ARG RDS_CA_BUNDLE_SHA256
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
USER temporal
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=15 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:7243/api/v1/namespaces/temporal-system"]
