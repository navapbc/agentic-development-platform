# syntax=docker/dockerfile:1

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
FROM ${AL2023_IMAGE} AS build
ARG AL2023_RELEASE=2023.12.20261112
ARG GO_VERSION=1.27.4
ARG GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
ARG GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
ARG TARGETARCH
RUN dnf -y --releasever="${AL2023_RELEASE}" install tar gzip \
 && dnf clean all \
 && case "${TARGETARCH}" in amd64) sum="${GO_SHA256_AMD64}" ;; arm64) sum="${GO_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local
WORKDIR /src/tls-sidecar
COPY docker/tls-sidecar/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/tls-sidecar .
WORKDIR /src/healthprobe
COPY docker/healthprobe/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM scratch
LABEL org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /out/tls-sidecar /out/healthprobe /usr/local/bin/
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/tls-sidecar"]
