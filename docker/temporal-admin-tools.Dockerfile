# syntax=docker/dockerfile:1

# The Temporal admin tools the platform runs, built from their release tags on Amazon Linux 2023
# with the Go cryptographic module GOFIPS140 names: temporal-sql-tool and the PostgreSQL schemas
# for the schema step, the temporal CLI for the namespace step, and tdbg for an operator.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b

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

FROM go AS tools
ARG TEMPORAL_VERSION=v1.32.2
ARG TEMPORAL_GO_GET=github.com/klauspost/compress@v1.20.2,golang.org/x/net@v0.62.0
ARG GOFIPS140=v1.0.0
RUN git clone --quiet --depth 1 --branch "${TEMPORAL_VERSION}" https://github.com/temporalio/temporal /src
WORKDIR /src
ENV GOFLAGS=-mod=mod
RUN go get $(printf '%s' "${TEMPORAL_GO_GET}" | tr , ' ') \
 && for tool in sql:temporal-sql-tool tdbg:tdbg; do \
      GOFIPS140="${GOFIPS140}" go build -trimpath -tags disable_grpc_modules -ldflags="-s -w" \
        -o "/out/${tool#*:}" "./cmd/tools/${tool%%:*}" || exit 1; \
    done \
 && mkdir -p /out/schema \
 && cp -r schema/postgresql /out/schema/postgresql

FROM go AS cli
ARG TEMPORAL_CLI_VERSION=v1.10.0
ARG TEMPORAL_CLI_GO_GET=github.com/klauspost/compress@v1.20.2,golang.org/x/net@v0.62.0
ARG GOFIPS140=v1.0.0
RUN git clone --quiet --depth 1 --branch "${TEMPORAL_CLI_VERSION}" https://github.com/temporalio/cli /src
WORKDIR /src
ENV GOFLAGS=-mod=mod
RUN go get $(printf '%s' "${TEMPORAL_CLI_GO_GET}" | tr , ' ') \
 && GOFIPS140="${GOFIPS140}" go build -trimpath \
      -ldflags="-s -w -X github.com/temporalio/cli/internal/temporalcli.Version=${TEMPORAL_CLI_VERSION#v}" \
      -o /out/temporal ./cmd/temporal

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
ARG RDS_CA_BUNDLE_URL
ARG RDS_CA_BUNDLE_SHA256
LABEL org.opencontainers.image.licenses="MIT"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install tzdata shadow-utils \
 && groupadd --gid 1000 temporal \
 && useradd --uid 1000 --gid 1000 --create-home --shell /sbin/nologin temporal \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=tools --chmod=0555 /out/temporal-sql-tool /out/tdbg /usr/local/bin/
COPY --from=cli --chmod=0555 /out/temporal /usr/local/bin/temporal
COPY --from=tools /out/schema /etc/temporal/schema
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
COPY --chmod=0555 docker/temporal-admin-tools.sh /usr/local/libexec/temporal-admin-tools.sh
RUN cis-harden
USER temporal
ENTRYPOINT ["/bin/sh", "/usr/local/libexec/temporal-admin-tools.sh"]
CMD ["schema"]
