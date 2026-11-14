# syntax=docker/dockerfile:1

# The Temporal server built from its release tag on Amazon Linux 2023, with the Go cryptographic
# module GOFIPS140 names, so GODEBUG=fips140=on runs it on that module.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b

FROM ${AL2023_IMAGE} AS go
ARG AL2023_RELEASE=2023.12.20261112
ARG GO_VERSION=1.27.4
ARG GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
ARG GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
ARG TARGETARCH
RUN dnf -y --releasever="${AL2023_RELEASE}" install tar gzip git gawk grep \
 && dnf clean all \
 && case "${TARGETARCH}" in amd64) sum="${GO_SHA256_AMD64}" ;; arm64) sum="${GO_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local

FROM go AS healthprobe
ARG GOFIPS140=v1.0.0
WORKDIR /src
COPY docker/healthprobe/ ./
RUN GOFIPS140="${GOFIPS140}" go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

# The embedded template takes one TEMPORAL_TLS_REQUIRE_CLIENT_AUTH for the internode and the
# frontend listeners. The deployed server requires client certificates on internode traffic
# only, so the frontend listener reads its own variable.
FROM go AS server
ARG TEMPORAL_VERSION=v1.32.2
ARG TEMPORAL_GO_GET=github.com/klauspost/compress@v1.20.2,golang.org/x/net@v0.62.0
ARG GOFIPS140=v1.0.0
RUN git clone --quiet --depth 1 --branch "${TEMPORAL_VERSION}" https://github.com/temporalio/temporal /src
WORKDIR /src
ENV GOFLAGS=-mod=mod
RUN go get $(printf '%s' "${TEMPORAL_GO_GET}" | tr , ' ') \
 && GOFIPS140="${GOFIPS140}" go build -trimpath -tags disable_grpc_modules -ldflags="-s -w" \
      -o /out/temporal-server ./cmd/server \
 && awk '/requireClientAuth:/ { seen++; if (seen == 2) sub(/TEMPORAL_TLS_REQUIRE_CLIENT_AUTH/, "TEMPORAL_TLS_FRONTEND_REQUIRE_CLIENT_AUTH") } { print }' \
      common/config/config_template_embedded.yaml > /out/platform-config.yaml \
 && [ "$(grep -c TEMPORAL_TLS_FRONTEND_REQUIRE_CLIENT_AUTH /out/platform-config.yaml)" = 1 ] \
 && cp docker/scripts/sh/entrypoint.sh /out/entrypoint.sh

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
ARG RDS_CA_BUNDLE_URL
ARG RDS_CA_BUNDLE_SHA256
LABEL org.opencontainers.image.licenses="MIT"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install gawk hostname tzdata shadow-utils \
 && groupadd --gid 1000 temporal \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /etc/temporal --shell /sbin/nologin temporal \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=server --chmod=0555 /out/temporal-server /usr/local/bin/temporal-server
COPY --from=server --chmod=0555 /out/entrypoint.sh /etc/temporal/entrypoint.sh
COPY --from=server --chmod=0444 /out/platform-config.yaml /etc/temporal/platform-config.yaml
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
COPY --chmod=0555 docker/temporal-server-start.sh /etc/temporal/platform-start.sh
WORKDIR /etc/temporal
RUN cis-harden
USER temporal
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=15 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:7243/api/v1/namespaces/temporal-system"]
CMD ["/etc/temporal/entrypoint.sh"]
