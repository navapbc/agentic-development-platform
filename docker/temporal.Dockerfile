# syntax=docker/dockerfile:1

ARG TEMPORAL_SERVER_IMAGE=docker.io/temporalio/server:1.32.1@sha256:acecbab5b0b8ae9cf4012b508866ca97feda3709577bf157b537239f40029049
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
ARG TEMPORAL_CONFIG_TEMPLATE_URL=https://raw.githubusercontent.com/temporalio/temporal/v1.32.1/common/config/config_template_embedded.yaml
ARG TEMPORAL_CONFIG_TEMPLATE_SHA256=8dc9ea0acf7f94b609fa4d1a11aa6d3c3e2c8afd8e7c6c72e50728d4f1fe1129

FROM public.ecr.aws/docker/library/golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS healthprobe
WORKDIR /src
COPY docker/healthprobe/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

# The embedded template takes one TEMPORAL_TLS_REQUIRE_CLIENT_AUTH for the internode and the
# frontend listeners. The deployed server requires client certificates on internode traffic
# only, so the frontend listener reads its own variable.
FROM ${TEMPORAL_SERVER_IMAGE} AS platform-config
ARG TEMPORAL_CONFIG_TEMPLATE_URL
ARG TEMPORAL_CONFIG_TEMPLATE_SHA256
USER root
ADD --checksum=sha256:${TEMPORAL_CONFIG_TEMPLATE_SHA256} ${TEMPORAL_CONFIG_TEMPLATE_URL} /tmp/template.yaml
RUN awk '/requireClientAuth:/ { seen++; if (seen == 2) sub(/TEMPORAL_TLS_REQUIRE_CLIENT_AUTH/, "TEMPORAL_TLS_FRONTEND_REQUIRE_CLIENT_AUTH") } { print }' /tmp/template.yaml > /tmp/platform.yaml \
    && [ "$(grep -c TEMPORAL_TLS_FRONTEND_REQUIRE_CLIENT_AUTH /tmp/platform.yaml)" = 1 ]

FROM ${TEMPORAL_SERVER_IMAGE}
ARG RDS_CA_BUNDLE_URL
ARG RDS_CA_BUNDLE_SHA256
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
COPY --from=platform-config --chmod=0444 /tmp/platform.yaml /etc/temporal/platform-config.yaml
COPY --chmod=0555 docker/temporal-server-start.sh /etc/temporal/platform-start.sh
USER temporal
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=15 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:7243/api/v1/namespaces/temporal-system"]
