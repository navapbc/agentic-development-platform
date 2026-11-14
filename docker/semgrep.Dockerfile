# syntax=docker/dockerfile:1

# The semgrep scanner image the scanner pods run. The pod runs `semgrep` from PATH against the
# mounted workspace. docker/semgrep/requirements.txt locks semgrep and its dependencies by hash.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85

FROM ${UV_IMAGE} AS uv

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.13 git \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY docker/semgrep/requirements.txt /tmp/semgrep-requirements.txt
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv venv --quiet --python /usr/bin/python3.13 /opt/semgrep \
 && VIRTUAL_ENV=/opt/semgrep uv pip sync --quiet --no-cache --require-hashes /tmp/semgrep-requirements.txt \
 && rm /tmp/semgrep-requirements.txt \
 && ln -s /opt/semgrep/bin/semgrep /usr/local/bin/semgrep \
 && ln -s /opt/semgrep/bin/pysemgrep /usr/local/bin/pysemgrep
WORKDIR /src
RUN cis-harden
CMD ["semgrep", "--help"]
