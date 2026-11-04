# syntax=docker/dockerfile:1

ARG TEMPORAL_UI_IMAGE=docker.io/temporalio/ui:2.58.0@sha256:d36185e811d0ce1720b8d3ca4d9ac4dac504b67181e9b5e3d3ca72b2294f3590

FROM ${TEMPORAL_UI_IMAGE}
USER root
RUN apk add --no-cache openssl
COPY --chmod=0555 docker/temporal-ui-start.sh /usr/local/libexec/temporal-ui-start.sh
USER temporal
ENTRYPOINT ["/usr/local/libexec/temporal-ui-start.sh"]
