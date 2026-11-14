ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.12 git shadow-utils \
 && ln -s /usr/bin/python3.12 /usr/local/bin/python3 \
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
