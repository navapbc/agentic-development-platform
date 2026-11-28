ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:75a2d4226d9b98c914d7eed878a03bef4f365ad548be5d28d464ccba504cec79

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261120
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.14 git shadow-utils \
 && ln -s /usr/bin/python3.14 /usr/local/bin/python3 \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin engine \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf

COPY bin /engine/bin
COPY policy.py rules.toml /engine/

RUN cis-harden
USER engine
WORKDIR /workspace
HEALTHCHECK NONE
