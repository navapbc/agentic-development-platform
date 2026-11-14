# syntax=docker/dockerfile:1

# The trivy scanner image the scanner pods run. The pod runs `trivy` from PATH against the
# mounted workspace, and trivy downloads its vulnerability database at run time.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE} AS go
ARG AL2023_RELEASE=2023.12.20261112
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

FROM go AS trivy
ARG TRIVY_MODE=source
ARG TRIVY_VERSION=v0.76.2
ARG TRIVY_SHA256_AMD64=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf
ARG TRIVY_SHA256_ARM64=ab9d51f86e6a5fb936f605faff6b593ac51bc378fff4d3e8b9d58358b9c367c3
ARG TRIVY_GO_GET=github.com/klauspost/compress@v1.20.2
ARG TARGETARCH
RUN NAME=trivy \
    MODE="${TRIVY_MODE}" \
    VERSION="${TRIVY_VERSION}" \
    SHA256_AMD64="${TRIVY_SHA256_AMD64}" \
    SHA256_ARM64="${TRIVY_SHA256_ARM64}" \
    GO_GET="${TRIVY_GO_GET}" \
    RELEASE_URL='https://github.com/aquasecurity/trivy/releases/download/{version}/trivy_{v}_Linux-{release_arch}.tar.gz' \
    RELEASE_MEMBER='trivy' \
    ARCH_NAMES=amd64=64bit,arm64=ARM64 \
    REPOSITORY=https://github.com/aquasecurity/trivy \
    PACKAGE=./cmd/trivy \
    LDFLAGS='-X github.com/aquasecurity/trivy/pkg/version/app.ver={v}' \
    go-component

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=trivy /out/trivy /usr/local/bin/trivy
RUN cis-harden
ENTRYPOINT ["/usr/local/bin/trivy"]
