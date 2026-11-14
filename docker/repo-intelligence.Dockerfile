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

FROM go AS scip
ARG SCIP_MODE=source
ARG SCIP_VERSION=v0.10.0
ARG SCIP_SHA256_AMD64=eeb28ebbff443c01609fb1691809958f3f0c7a8651af689a5bc8560061cd8ab0
ARG SCIP_SHA256_ARM64=6ab677dc2c4bf2955975d0530766152e45daaa988f9404068d8adecacd0bb24c
ARG SCIP_GO_GET=github.com/klauspost/compress@v1.20.2,golang.org/x/text@v0.41.0
ARG TARGETARCH
RUN NAME=scip \
    MODE="${SCIP_MODE}" \
    VERSION="${SCIP_VERSION}" \
    SHA256_AMD64="${SCIP_SHA256_AMD64}" \
    SHA256_ARM64="${SCIP_SHA256_ARM64}" \
    GO_GET="${SCIP_GO_GET}" \
    RELEASE_URL='https://github.com/scip-code/scip/releases/download/{version}/scip-linux-{arch}.tar.gz' \
    RELEASE_MEMBER='scip' \
    REPOSITORY=https://github.com/scip-code/scip \
    PACKAGE=./cmd/scip \
    LDFLAGS='' \
    go-component

FROM go AS terraform-ls
ARG TERRAFORM_LS_MODE=source
ARG TERRAFORM_LS_VERSION=v0.39.1
ARG TERRAFORM_LS_SHA256_AMD64=fe2a158e4a1a8ec8047dae9e06c439b468412aa579929ba28b2e1258cedef9c0
ARG TERRAFORM_LS_SHA256_ARM64=a72a6d9f72580cfc30cc06d3c973fd5f55790f62e3ddb195998435070d9af36d
ARG TERRAFORM_LS_GO_GET=
ARG TARGETARCH
RUN NAME=terraform-ls \
    MODE="${TERRAFORM_LS_MODE}" \
    VERSION="${TERRAFORM_LS_VERSION}" \
    SHA256_AMD64="${TERRAFORM_LS_SHA256_AMD64}" \
    SHA256_ARM64="${TERRAFORM_LS_SHA256_ARM64}" \
    GO_GET="${TERRAFORM_LS_GO_GET}" \
    RELEASE_URL='https://releases.hashicorp.com/terraform-ls/{v}/terraform-ls_{v}_linux_{arch}.zip' \
    RELEASE_MEMBER='terraform-ls' \
    REPOSITORY=https://github.com/hashicorp/terraform-ls \
    PACKAGE=. \
    LDFLAGS='' \
    go-component

FROM go AS zoekt
ARG ZOEKT_COMMIT=2cb19912a4073e5a9895658b7cb135ee4b35733b
ARG ZOEKT_GO_GET=golang.org/x/net@v0.62.0,google.golang.org/grpc@v1.83.2
# zoekt publishes no releases, so the pin is a commit.
RUN git init --quiet /src \
 && cd /src \
 && git fetch --quiet --depth 1 https://github.com/sourcegraph/zoekt "${ZOEKT_COMMIT}" \
 && git checkout --quiet FETCH_HEAD \
 && GOFLAGS=-mod=mod go get $(printf '%s' "${ZOEKT_GO_GET}" | tr , ' ') \
 && GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" -o /out/zoekt ./cmd/zoekt \
 && GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w" -o /out/zoekt-index ./cmd/zoekt-index

FROM go AS downloads
ARG SCIP_JAVA_VERSION=v0.14.2
ARG SCIP_JAVA_SHA256=6620a8f2af822ccfa23fbf8a852560edd05c3834dc8cf779ca507abce71586dc
ARG PHPACTOR_VERSION=2026.10.07.0
ARG PHPACTOR_SHA256=b28bf6166e016d539fc2f2aec18ef918b12590e436a6f1ccbdf30b5bbdbdb16b
ARG SCIP_RUBY_VERSION=scip-ruby-v0.5.1
ARG SCIP_RUBY_SHA256=3b0eec58c00d2c742e182fc9070a048d933ff6485098bcc3e3c8b28fbcf32c8e
RUN mkdir -p /out/bin \
 && curl -fsSLo /out/bin/scip-java "https://github.com/scip-code/scip-java/releases/download/${SCIP_JAVA_VERSION}/scip-java-${SCIP_JAVA_VERSION}" \
 && echo "${SCIP_JAVA_SHA256}  /out/bin/scip-java" | sha256sum -c - \
 && curl -fsSLo /out/bin/phpactor "https://github.com/phpactor/phpactor/releases/download/${PHPACTOR_VERSION}/phpactor.phar" \
 && echo "${PHPACTOR_SHA256}  /out/bin/phpactor" | sha256sum -c - \
 && curl -fsSLo /out/bin/scip-ruby "https://github.com/sourcegraph/scip-ruby/releases/download/${SCIP_RUBY_VERSION}/scip-ruby-x86_64-linux" \
 && echo "${SCIP_RUBY_SHA256}  /out/bin/scip-ruby" | sha256sum -c - \
 && chmod 0555 /out/bin/*

FROM ${AL2023_IMAGE} AS runtime
ARG AL2023_RELEASE=2023.12.20261112
ARG AST_GREP_VERSION=0.51.2
ARG SCIP_PYTHON_VERSION=0.6.8
ARG SCIP_PYTHON_TYPESCRIPT_VERSION=4.9.5
ARG SCIP_TYPESCRIPT_VERSION=0.6.2
LABEL org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLATFORM_IN_CONTAINER=1 \
    BIND_HOST=0.0.0.0 \
    VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto \
    PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.13 git nodejs24 nodejs24-npm \
      php8.4-cli php8.4-mbstring php8.4-xml java-21-amazon-corretto-devel maven-amazon-corretto21 shadow-utils \
 && npm install --global --prefix /usr/local "@ast-grep/cli@${AST_GREP_VERSION}" \
 && npm install --global --prefix /usr/local "@sourcegraph/scip-typescript@${SCIP_TYPESCRIPT_VERSION}" \
 && mkdir -p /opt/scip-python \
 && printf '{"private":true,"dependencies":{"@sourcegraph/scip-python":"%s"},"overrides":{"typescript":"%s"}}' \
      "${SCIP_PYTHON_VERSION}" "${SCIP_PYTHON_TYPESCRIPT_VERSION}" > /opt/scip-python/package.json \
 && npm install --prefix /opt/scip-python --omit=dev \
 && ln -s /opt/scip-python/node_modules/.bin/scip-python /usr/local/bin/scip-python \
 && npm cache clean --force \
 && useradd --create-home --uid 10001 appuser \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=zoekt /out/zoekt /out/zoekt-index /usr/local/bin/
COPY --from=scip /out/scip /usr/local/bin/scip
COPY --from=terraform-ls /out/terraform-ls /usr/local/bin/terraform-ls
COPY --from=downloads /out/bin/ /usr/local/bin/
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
# basedpyright, a type checker in the dev extra, ships a Node runtime the service never runs.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra=dev --no-install-project \
      --no-install-package basedpyright --no-install-package nodejs-wheel-binaries
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --extra=dev \
      --no-install-package basedpyright --no-install-package nodejs-wheel-binaries \
 && rm /usr/local/bin/uv \
 && mkdir -p /app/repository_indexes \
 && chown -R 10001:10001 /app/repository_indexes
COPY --chmod=0555 docker/platform-entrypoint.sh /usr/local/bin/platform-entrypoint
RUN cis-harden
USER appuser
ENTRYPOINT ["/usr/local/bin/platform-entrypoint"]
CMD ["python", "-m", "services.repository_intelligence.server"]
