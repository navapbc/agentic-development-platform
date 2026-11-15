# syntax=docker/dockerfile:1

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

FROM go AS kubectl
ARG KUBECTL_MODE=source
ARG KUBECTL_VERSION=v1.37.2
ARG KUBECTL_SHA256_AMD64=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4
ARG KUBECTL_SHA256_ARM64=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
ARG KUBECTL_GO_GET=golang.org/x/net@v0.62.0
ARG TARGETARCH
RUN NAME=kubectl \
    MODE="${KUBECTL_MODE}" \
    VERSION="${KUBECTL_VERSION}" \
    SHA256_AMD64="${KUBECTL_SHA256_AMD64}" \
    SHA256_ARM64="${KUBECTL_SHA256_ARM64}" \
    GO_GET="${KUBECTL_GO_GET}" \
    RELEASE_URL='https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl' \
    REPOSITORY=https://github.com/kubernetes/kubernetes \
    PACKAGE=./cmd/kubectl \
    LDFLAGS='-X k8s.io/component-base/version.gitVersion={version} -X k8s.io/client-go/pkg/version.gitVersion={version}' \
    go-component

FROM ${AL2023_IMAGE} AS build
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" install python3.12 git tar gzip \
 && dnf clean all
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra worker --no-install-project


COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts
COPY observability ./observability
COPY openhands ./openhands
COPY vendor ./vendor
COPY config/platform ./config/platform

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra worker


FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.12 git openssh-clients tar shadow-utils \
 && useradd --create-home --uid 10001 appuser \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLATFORM_IN_CONTAINER=1 \
    BIND_HOST=0.0.0.0 \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
COPY --from=kubectl /out/kubectl /usr/local/bin/kubectl
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
WORKDIR /app
ARG AGENT_BUILD_ID=0.3.0
ENV AGENT_BUILD_ID=${AGENT_BUILD_ID}
COPY --chmod=0555 docker/platform-entrypoint.sh /usr/local/bin/platform-entrypoint
RUN cis-harden
USER appuser
ENTRYPOINT ["/usr/local/bin/platform-entrypoint"]
CMD ["python", "-m", "services.orchestration.worker"]
