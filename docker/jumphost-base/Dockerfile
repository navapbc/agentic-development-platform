ARG BASE_IMAGE=public.ecr.aws/docker/library/alpine:3.20
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=repo LICENSE NOTICE /usr/share/doc/agentic-dev-platform/
COPY entrypoint.sh /bin/entrypoint.sh
RUN chmod 0755 /bin/entrypoint.sh

ENTRYPOINT ["/bin/entrypoint.sh"]
