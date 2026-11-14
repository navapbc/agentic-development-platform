# syntax=docker/dockerfile:1

# The Temporal UI server built from the ui-server release tag, which carries the built web
# assets, on Amazon Linux 2023 with the Go cryptographic module GOFIPS140 names.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE} AS go
ARG AL2023_RELEASE=2023.12.20261112
ARG GO_VERSION=1.27.4
ARG GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
ARG GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
ARG TARGETARCH
RUN dnf -y --releasever="${AL2023_RELEASE}" install tar gzip git \
 && dnf clean all \
 && case "${TARGETARCH}" in amd64) sum="${GO_SHA256_AMD64}" ;; arm64) sum="${GO_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local

FROM go AS ui
ARG TEMPORAL_UI_VERSION=v2.58.1
ARG TEMPORAL_UI_GO_GET=golang.org/x/net@v0.62.0
ARG GOFIPS140=v1.0.0
RUN git clone --quiet --depth 1 --branch "${TEMPORAL_UI_VERSION}" https://github.com/temporalio/ui-server /src
WORKDIR /src
ENV GOFLAGS=-mod=mod
RUN go get $(printf '%s' "${TEMPORAL_UI_GO_GET}" | tr , ' ') \
 && GOFIPS140="${GOFIPS140}" go build -trimpath -ldflags="-s -w" -o /out/ui-server ./cmd/server \
 && cp config/docker.yaml /out/docker.yaml

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="MIT"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install openssl tzdata shadow-utils \
 && groupadd --gid 1000 temporal \
 && useradd --uid 1000 --gid 1000 --create-home --shell /sbin/nologin temporal \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
WORKDIR /home/ui-server
COPY --from=ui --chmod=0555 /out/ui-server ./ui-server
COPY --from=ui /out/docker.yaml ./config/docker.yaml
COPY --chmod=0555 docker/temporal-ui-start.sh /usr/local/libexec/temporal-ui-start.sh
ENV TEMPORAL_CLOUD_UI=false
RUN cis-harden
USER temporal
EXPOSE 8080
ENTRYPOINT ["/usr/local/libexec/temporal-ui-start.sh"]
