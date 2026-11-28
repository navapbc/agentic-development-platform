ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:75a2d4226d9b98c914d7eed878a03bef4f365ad548be5d28d464ccba504cec79

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261120
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf clean all \
 && rm -rf /var/cache/dnf
LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY LICENSE NOTICE /usr/share/doc/agentic-dev-platform/
COPY docker/jumphost-base/entrypoint.sh /bin/entrypoint.sh
RUN chmod 0755 /bin/entrypoint.sh

RUN cis-harden
USER 65534:65534
HEALTHCHECK NONE

ENTRYPOINT ["/bin/entrypoint.sh"]
