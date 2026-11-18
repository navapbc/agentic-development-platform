# syntax=docker/dockerfile:1

# The catalog sandbox images the coding agent runs in. agent-server-python-node carries Python
# 3.13 and Node 24, and agent-server-jvm and agent-server-go add a JDK with Maven and a Go
# toolchain to it, each from the Amazon Linux packages. The agent runs in CLI mode, which
# disables the browser tools, so the images carry no browser, no editor server, and no kernel
# packages.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85

FROM ${UV_IMAGE} AS uv

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

FROM go AS tini
ARG TINI_VERSION=v0.19.0
ARG TINI_SHA256_AMD64=c5b0666b4cb676901f90dfcb37106783c5fe2077b04590973b885950611b30ee
ARG TINI_SHA256_ARM64=eae1d3aa50c48fb23b8cbdf4e369d0910dfc538566bfd09df89a774aa84a48b9
ARG TARGETARCH
RUN case "${TARGETARCH}" in amd64) sum="${TINI_SHA256_AMD64}" ;; arm64) sum="${TINI_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /out/tini --create-dirs "https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini-static-${TARGETARCH}" \
 && echo "${sum}  /out/tini" | sha256sum -c - \
 && chmod 0555 /out/tini

FROM ${AL2023_IMAGE} AS agent-server-python-node
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.13 python3.13-pip nodejs24 nodejs24-npm \
      git tmux bash tar gzip unzip procps-ng findutils which file jq diffutils shadow-utils \
 && useradd --uid 10001 --create-home openhands \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf \
 && ln -s /usr/bin/python3.13 /usr/local/bin/python3 \
 && ln -s /usr/bin/python3.13 /usr/local/bin/python \
 && ln -s /usr/bin/pip3.13 /usr/local/bin/pip3 \
 && ln -s /usr/bin/pip3.13 /usr/local/bin/pip
COPY docker/agent-server/requirements.txt /tmp/agent-server-requirements.txt
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv venv --quiet --python /usr/bin/python3.13 /opt/agent-server \
 && VIRTUAL_ENV=/opt/agent-server uv pip sync --quiet --no-cache --require-hashes \
      /tmp/agent-server-requirements.txt \
 && rm /tmp/agent-server-requirements.txt \
 && ln -s /opt/agent-server/bin/agent-server /usr/local/bin/openhands-agent-server
COPY --from=tini /out/tini /usr/local/bin/tini
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    LOG_JSON=true \
    OH_ENABLE_VSCODE=false \
    OH_PRELOAD_TOOLS=false
RUN cis-harden
USER openhands
EXPOSE 8000
HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/tini", "--", "/usr/local/bin/openhands-agent-server"]

FROM agent-server-python-node AS agent-server-jvm
ARG AL2023_RELEASE=2023.12.20261112
USER root
RUN dnf -y --releasever="${AL2023_RELEASE}" install java-21-amazon-corretto-devel maven-amazon-corretto21 \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto
RUN cis-harden
USER openhands

FROM agent-server-python-node AS agent-server-go
ARG AL2023_RELEASE=2023.12.20261112
USER root
RUN dnf -y --releasever="${AL2023_RELEASE}" install golang \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV GOTOOLCHAIN=local
RUN cis-harden
USER openhands
