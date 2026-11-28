# syntax=docker/dockerfile:1

# The osv-scanner image the scanner pods run. It carries the OSV databases under
# /opt/osv-scanner/db, so a scan without egress still works, and a scan with egress downloads
# fresh ones into /opt/osv-scanner/fresh.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:75a2d4226d9b98c914d7eed878a03bef4f365ad548be5d28d464ccba504cec79

FROM ${AL2023_IMAGE} AS go
ARG AL2023_RELEASE=2023.12.20261120
ARG GO_VERSION=1.27.4
ARG GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
ARG GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
ARG TARGETARCH
RUN dnf -y --releasever="${AL2023_RELEASE}" install tar gzip unzip git \
 && dnf clean all \
 && case "${TARGETARCH}" in amd64) sum="${GO_SHA256_AMD64}" ;; arm64) sum="${GO_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local
COPY --chmod=0555 docker/al2023/go-component.sh /usr/local/bin/go-component

FROM go AS osv-scanner
ARG OSV_SCANNER_MODE=source
ARG OSV_SCANNER_VERSION=v2.7.1
ARG OSV_SCANNER_SHA256_AMD64=fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8
ARG OSV_SCANNER_SHA256_ARM64=983d22fbad9724514e3fcc6acc83b5fa64c5ffa6b0d9f510028f22c1b223ff81
ARG OSV_SCANNER_GO_GET=
ARG TARGETARCH
RUN NAME=osv-scanner \
    MODE="${OSV_SCANNER_MODE}" \
    VERSION="${OSV_SCANNER_VERSION}" \
    SHA256_AMD64="${OSV_SCANNER_SHA256_AMD64}" \
    SHA256_ARM64="${OSV_SCANNER_SHA256_ARM64}" \
    GO_GET="${OSV_SCANNER_GO_GET}" \
    RELEASE_URL='https://github.com/google/osv-scanner/releases/download/{version}/osv-scanner_linux_{arch}' \
    REPOSITORY=https://github.com/google/osv-scanner \
    PACKAGE=./cmd/osv-scanner \
    go-component

# osv-scanner reads the zip of each ecosystem from osv-scalibr/<ecosystem>/all.zip under
# OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY.
FROM ${AL2023_IMAGE} AS osv-db
RUN set -eu \
 && for ecosystem in npm PyPI Go Maven; do \
      mkdir -p "/opt/osv-scanner/db/osv-scalibr/${ecosystem}"; \
      curl -fsSLo "/opt/osv-scanner/db/osv-scalibr/${ecosystem}/all.zip" \
        "https://osv-vulnerabilities.storage.googleapis.com/${ecosystem}/all.zip"; \
    done \
 && chmod -R a+rX,go-w /opt/osv-scanner

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261120
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install shadow-utils \
 && groupadd --gid 10001 osv-scanner \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/osv-scanner --shell /sbin/nologin osv-scanner \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=osv-scanner /out/osv-scanner /usr/local/bin/osv-scanner
COPY --from=osv-db /opt/osv-scanner /opt/osv-scanner
RUN install -d -o 10001 -g 10001 -m 0700 /opt/osv-scanner/fresh \
 && cis-harden
USER 10001:10001
HEALTHCHECK NONE
