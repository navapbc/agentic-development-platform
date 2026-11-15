ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf clean all \
 && rm -rf /var/cache/dnf
LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=repo LICENSE NOTICE /usr/share/doc/agentic-dev-platform/
COPY entrypoint.sh /bin/entrypoint.sh
RUN chmod 0755 /bin/entrypoint.sh

RUN cis-harden
USER 65534:65534

ENTRYPOINT ["/bin/entrypoint.sh"]
