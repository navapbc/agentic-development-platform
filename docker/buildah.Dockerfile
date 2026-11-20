# syntax=docker/dockerfile:1

# The builder the gate, checks, and scanner pods start from: buildah, plus notation and the AWS
# Signer plugin, which verify a signed image's signature immediately before the pod pulls it.
# The plugin is published only under a "latest" path, so its checksum pins the release, and a new
# release fails the build until the checksum is updated.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG SIGNER_PLUGIN_SHA256_ARM64=b416954ac98a20d71f968c4f7cd7b13905328352f43298a405a8261016a8be51
ARG SIGNER_PLUGIN_SHA256_AMD64=f8efa548932edc4851f4ba464496994bad753cdf21cd32baa575f7291a26c7b9
ARG SIGNER_ROOT_SHA256=0ccf529561f610b9eb7f9b0ee02fffe2f74f7ffdf0ac56348ca224e879461c9b

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

FROM go AS notation
ARG NOTATION_MODE=source
ARG NOTATION_VERSION=v1.3.2
ARG NOTATION_SHA256_AMD64=e1a0f060308086bf8020b2d31defb7c5348f133ca0dba6a1a7820ef3cbb6dfe5
ARG NOTATION_SHA256_ARM64=34143c7b816a50104824278080a5cf12bbf855488e003f62e6acf27ed25c423c
ARG NOTATION_GO_GET=golang.org/x/crypto@v0.56.0,oras.land/oras-go/v2@v2.6.2,golang.org/x/mod@v0.40.0
ARG TARGETARCH
RUN NAME=notation \
    MODE="${NOTATION_MODE}" \
    VERSION="${NOTATION_VERSION}" \
    SHA256_AMD64="${NOTATION_SHA256_AMD64}" \
    SHA256_ARM64="${NOTATION_SHA256_ARM64}" \
    GO_GET="${NOTATION_GO_GET}" \
    RELEASE_URL='https://github.com/notaryproject/notation/releases/download/{version}/notation_{v}_linux_{arch}.tar.gz' \
    RELEASE_MEMBER='notation' \
    REPOSITORY=https://github.com/notaryproject/notation \
    PACKAGE=./cmd/notation \
    LDFLAGS='' \
    go-component

FROM go AS signer-plugin
ARG SIGNER_PLUGIN_SHA256_ARM64
ARG SIGNER_PLUGIN_SHA256_AMD64
ARG SIGNER_ROOT_SHA256
ARG TARGETARCH
RUN case "${TARGETARCH}" in amd64) sum="${SIGNER_PLUGIN_SHA256_AMD64}" ;; arm64) sum="${SIGNER_PLUGIN_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/plugin.zip "https://d2hvyiie56hcat.cloudfront.net/linux/${TARGETARCH}/plugin/latest/notation-aws-signer-plugin.zip" \
 && echo "${sum}  /tmp/plugin.zip" | sha256sum -c - \
 && curl -fsSLo /tmp/aws-signer-notation-root.crt https://d2hvyiie56hcat.cloudfront.net/aws-signer-notation-root.cert \
 && echo "${SIGNER_ROOT_SHA256}  /tmp/aws-signer-notation-root.crt" | sha256sum -c - \
 && mkdir -p /out/notation/plugins/com.amazonaws.signer.notation.plugin /out/notation/truststore/x509/signingAuthority/aws-signer-ts \
 && unzip -q /tmp/plugin.zip notation-com.amazonaws.signer.notation.plugin -d /out/notation/plugins/com.amazonaws.signer.notation.plugin \
 && chmod 0555 /out/notation/plugins/com.amazonaws.signer.notation.plugin/notation-com.amazonaws.signer.notation.plugin \
 && cp /tmp/aws-signer-notation-root.crt /out/notation/truststore/x509/signingAuthority/aws-signer-ts/

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install buildah git tar gzip findutils diffutils gawk \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV BUILDAH_ISOLATION=chroot
COPY --from=notation /out/notation /usr/local/bin/notation
COPY --from=signer-plugin /out/notation /root/.config/notation
RUN notation version \
 && notation plugin list | grep -q com.amazonaws.signer.notation.plugin \
 && buildah --version
RUN cis-harden
