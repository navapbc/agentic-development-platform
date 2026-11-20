# syntax=docker/dockerfile:1

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

FROM go AS healthprobe
WORKDIR /src
COPY docker/healthprobe/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM go AS opa
ARG OPA_MODE=source
ARG OPA_VERSION=v1.22.4
ARG OPA_SHA256_AMD64=35e3b352e715e80624ead6c89930d8585f6f7d94cdc409eb2a9fe36620dcccba
ARG OPA_SHA256_ARM64=9e9afee01319a5044790e59b1a2ece39a943f9b6059b97bd86c7aab88830d833
ARG OPA_GO_GET=golang.org/x/net@v0.62.0
ARG TARGETARCH
RUN NAME=opa \
    MODE="${OPA_MODE}" \
    VERSION="${OPA_VERSION}" \
    SHA256_AMD64="${OPA_SHA256_AMD64}" \
    SHA256_ARM64="${OPA_SHA256_ARM64}" \
    GO_GET="${OPA_GO_GET}" \
    RELEASE_URL='https://github.com/open-policy-agent/opa/releases/download/{version}/opa_linux_{arch}_static' \
    REPOSITORY=https://github.com/open-policy-agent/opa \
    PACKAGE=. \
    LDFLAGS='' \
    go-component

FROM scratch
LABEL org.opencontainers.image.licenses="Apache-2.0"
COPY --from=go /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem /etc/ssl/certs/ca-certificates.crt
COPY --from=opa /out/opa /opa
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
USER 1000:1000
ENTRYPOINT ["/opa"]
CMD ["run"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:8181/health"]
