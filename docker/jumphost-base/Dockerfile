ARG BASE_IMAGE=public.ecr.aws/docker/library/alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=repo LICENSE NOTICE /usr/share/doc/agentic-dev-platform/
COPY entrypoint.sh /bin/entrypoint.sh
RUN chmod 0755 /bin/entrypoint.sh

USER 65534:65534

ENTRYPOINT ["/bin/entrypoint.sh"]
