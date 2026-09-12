ARG BASE_IMAGE=public.ecr.aws/docker/library/alpine:3.20
FROM ${BASE_IMAGE}

COPY entrypoint.sh /bin/entrypoint.sh
RUN chmod 0755 /bin/entrypoint.sh

ENTRYPOINT ["/bin/entrypoint.sh"]
