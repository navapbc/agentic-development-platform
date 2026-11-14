# syntax=docker/dockerfile:1

# Langfuse web and worker built from their release tag on Amazon Linux 2023, following the
# upstream web and worker Dockerfiles. LANGFUSE_OVERRIDES adds pnpm overrides, as name@range, to
# the workspace before the install, and the Prisma CLI the web entrypoint runs gets the same ones.

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

FROM go AS migrate
ARG GOLANG_MIGRATE_VERSION=v4.19.1
ARG GOFIPS140=v1.0.0
ENV GOFIPS140=${GOFIPS140}
RUN GOBIN=/out go install -trimpath -tags clickhouse -ldflags="-s -w" \
      "github.com/golang-migrate/migrate/v4/cmd/migrate@${GOLANG_MIGRATE_VERSION}"

FROM go AS dumb-init
ARG DUMB_INIT_VERSION=v1.2.5
ARG DUMB_INIT_SHA256_AMD64=e874b55f3279ca41415d290c512a7ba9d08f98041b28ae7c2acb19a545f1c4df
ARG DUMB_INIT_SHA256_ARM64=b7d648f97154a99c539b63c55979cd29f005f88430fb383007fe3458340b795e
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) arch=x86_64; sum="${DUMB_INIT_SHA256_AMD64}" ;; \
      arm64) arch=aarch64; sum="${DUMB_INIT_SHA256_ARM64}" ;; \
      *) exit 1 ;; \
    esac \
 && curl -fsSLo /out/dumb-init --create-dirs \
      "https://github.com/Yelp/dumb-init/releases/download/${DUMB_INIT_VERSION}/dumb-init_${DUMB_INIT_VERSION#v}_${arch}" \
 && echo "${sum}  /out/dumb-init" | sha256sum -c - \
 && chmod 0555 /out/dumb-init

FROM ${AL2023_IMAGE} AS source
ARG AL2023_RELEASE=2023.12.20261112
ARG LANGFUSE_VERSION=v4.79.0
ARG LANGFUSE_PNPM_VERSION=12.16.1
ARG LANGFUSE_TURBO_VERSION=2.11.9
ARG LANGFUSE_OVERRIDES=deepmerge-ts@^8.0.2
RUN dnf -y --releasever="${AL2023_RELEASE}" install nodejs24 nodejs24-npm git tar gzip gcc gcc-c++ make \
      python3 findutils \
 && dnf clean all \
 && npm install --global --prefix /usr/local "turbo@${LANGFUSE_TURBO_VERSION}" "pnpm@${LANGFUSE_PNPM_VERSION}"
ENV NEXT_TELEMETRY_DISABLED=1
RUN git clone --quiet --depth 1 --branch "${LANGFUSE_VERSION}" https://github.com/langfuse/langfuse /app
WORKDIR /app
RUN for override in $(printf '%s' "${LANGFUSE_OVERRIDES}" | tr , ' '); do \
      sed -i "/^overrides:$/a\\  \"${override%@*}\": \"${override##*@}\"" pnpm-workspace.yaml; \
    done \
 && pnpm install --lockfile-only

FROM source AS web-build
RUN turbo prune web --docker \
 && mkdir /build \
 && cp -r out/json/. /build/ \
 && cp out/pnpm-lock.yaml out/pnpm-workspace.yaml /build/
WORKDIR /build
RUN pnpm install --frozen-lockfile \
 && cp -r /app/out/full/. /build/ \
 && cp /app/scripts/docker/prune-node-modules.sh /usr/local/bin/prune-node-modules \
 && node ./packages/shared/clickhouse/scripts/prepare-migrations.mjs materialize ./packages/shared/clickhouse/migrations \
 && chmod 755 ./packages/shared/clickhouse/migrations/clustered ./packages/shared/clickhouse/migrations/unclustered \
 && rm -f ./web/src/middleware.ts
RUN mkdir -p ./scripts/vitest \
 && cp /app/scripts/vitest/ci-reporter.ts ./scripts/vitest/ci-reporter.ts
ENV pnpm_config_verify_deps_before_run=false \
    DOCKER_BUILD=1 \
    NEXT_MANUAL_SIG_HANDLE=true \
    CI=true
RUN NODE_OPTIONS='--max-old-space-size-percentage=75' turbo run build --filter=web... \
 && find ./web/.next/standalone -type f -name '*.nft.json' -delete \
 && sh /usr/local/bin/prune-node-modules ./web/.next/standalone/node_modules

FROM source AS prisma-cli
ARG LANGFUSE_PRISMA_VERSION=6.19.3
RUN mkdir -p /opt/prisma-cli \
 && overrides="$(printf '%s' "${LANGFUSE_OVERRIDES}" | tr , '\n' | sed 's/^\(.*\)@\(.*\)$/"\1":"\2"/' | paste -sd, -)" \
 && printf '{"private":true,"dependencies":{"prisma":"%s"},"overrides":{%s}}' "${LANGFUSE_PRISMA_VERSION}" "${overrides}" \
      > /opt/prisma-cli/package.json \
 && npm install --prefix /opt/prisma-cli --omit=dev --no-fund --no-audit \
 && rm -rf /opt/prisma-cli/node_modules/prisma/prisma-client /opt/prisma-cli/node_modules/prisma/build/public \
 && sh /app/scripts/docker/prune-node-modules.sh /opt/prisma-cli/node_modules

FROM source AS worker-build
ARG RUST_VERSION=1.98.0
ARG RUST_SHA256_AMD64=aa30409afa67bd1ada244cefd82c7980e6a65bc113bb978e934b2413c75e3900
ARG RUST_SHA256_ARM64=5fbb4282403046d52a4672765c6761a809bf9f33e699b17e6eb7a93ab7770cc3
ARG CARGO_ABOUT_VERSION=0.9.2
ARG CARGO_AUDITABLE_VERSION=0.7.7
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
      amd64) triple=x86_64-unknown-linux-gnu; sum="${RUST_SHA256_AMD64}" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sum="${RUST_SHA256_ARM64}" ;; \
      *) exit 1 ;; \
    esac \
 && curl -fsSLo /tmp/rust.tar.gz "https://static.rust-lang.org/dist/rust-${RUST_VERSION}-${triple}.tar.gz" \
 && echo "${sum}  /tmp/rust.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/rust.tar.gz -C /tmp \
 && "/tmp/rust-${RUST_VERSION}-${triple}/install.sh" --prefix=/usr/local --components="rustc,cargo,rust-std-${triple}" \
 && rm -rf /tmp/rust.tar.gz "/tmp/rust-${RUST_VERSION}-${triple}" \
 && cargo install --locked --root /usr/local --features cli "cargo-about@${CARGO_ABOUT_VERSION}" \
 && cargo install --locked --root /usr/local "cargo-auditable@${CARGO_AUDITABLE_VERSION}" \
 && rm -rf "${HOME}/.cargo/registry" \
 && printf '#!/bin/sh\nexec /usr/local/bin/cargo auditable "$@"\n' > /usr/local/bin/cargo-auditable-shim \
 && chmod 0755 /usr/local/bin/cargo-auditable-shim
RUN turbo prune worker --docker \
 && mkdir /build \
 && cp -r out/json/. /build/ \
 && cp out/pnpm-lock.yaml out/pnpm-workspace.yaml /build/
WORKDIR /build
ENV pnpm_config_verify_deps_before_run=false
RUN pnpm install --frozen-lockfile \
 && cp -r /app/out/full/. /build/ \
 && CARGO=/usr/local/bin/cargo-auditable-shim turbo run build --filter=worker... \
 && (cd packages/native \
     && mkdir -p /usr/share/licenses/langfuse-native \
     && cargo fetch --locked \
     && cargo about generate --frozen --fail about.hbs -o /usr/share/licenses/langfuse-native/THIRD_PARTY_LICENSES.txt) \
 && pnpm --filter worker deploy --prod /prod/worker \
 && builder_client="$(find /build/node_modules/.pnpm -path '*/node_modules/@prisma/client' -type d | head -n 1)" \
 && deployed_client="$(find /prod/worker/node_modules/.pnpm -path '*/node_modules/@prisma/client' -type d | head -n 1)" \
 && rm -rf "${deployed_client}" "$(dirname "$(dirname "${deployed_client}")")/.prisma" \
 && cp -R "${builder_client}" "${deployed_client}" \
 && cp -R "$(dirname "$(dirname "${builder_client}")")/.prisma" "$(dirname "$(dirname "${deployed_client}")")/.prisma" \
 && sh /app/scripts/docker/prune-node-modules.sh /prod/worker/node_modules \
 && ! ls /prod/worker/node_modules/.pnpm | grep -E '^(next|prisma|@prisma\+engines)@'

FROM ${AL2023_IMAGE} AS runtime
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install nodejs24 nodejs24-full-i18n bash sed tzdata crypto-policies wget shadow-utils \
 && groupadd --gid 1001 nodejs \
 && useradd --uid 1001 --gid 1001 --home-dir /home/nextjs --shell /sbin/nologin nextjs \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf \
 && sed 's#/etc/crypto-policies/back-ends/opensslcnf.config#/usr/share/crypto-policies/FIPS/opensslcnf.txt#' \
      /etc/pki/tls/openssl.cnf > /etc/pki/tls/openssl-fips.cnf \
 && grep -q '/usr/share/crypto-policies/FIPS/opensslcnf.txt' /etc/pki/tls/openssl-fips.cnf
COPY --from=dumb-init /out/dumb-init /usr/bin/dumb-init
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    DOCKER_BUILD=0 \
    NEXT_MANUAL_SIG_HANDLE=true

FROM runtime AS langfuse-web
LABEL org.opencontainers.image.licenses="MIT"
COPY --from=prisma-cli /opt/prisma-cli /opt/prisma-cli
RUN ln -s /opt/prisma-cli/node_modules/.bin/prisma /usr/local/bin/prisma
COPY --from=migrate /out/migrate /usr/bin/migrate
COPY --from=web-build --chown=nextjs:nodejs /build/web/next.config.mjs /build/web/package.json ./
COPY --from=web-build --chown=nextjs:nodejs /build/web/.next/standalone ./
COPY --from=web-build --chown=nextjs:nodejs /build/web/.next/static ./web/.next/static
COPY --from=web-build --chown=nextjs:nodejs /build/web/public ./web/public
COPY --from=web-build --chown=nextjs:nodejs /build/packages/shared/prisma ./packages/shared/prisma
COPY --from=web-build --chown=nextjs:nodejs /build/packages/shared/clickhouse ./packages/shared/clickhouse
COPY --from=web-build --chown=nextjs:nodejs /build/web/entrypoint.sh ./web/entrypoint.sh
COPY --from=web-build --chown=nextjs:nodejs /build/packages/shared/scripts/cleanup.sql ./packages/shared/scripts/cleanup.sql
RUN chmod +x ./web/entrypoint.sh
RUN cis-harden
USER 1001
ENV PORT=3000
ENTRYPOINT ["dumb-init", "--", "./web/entrypoint.sh"]
CMD ["node", "./web/server.js", "--keepAliveTimeout", "110000"]

FROM runtime AS langfuse-worker
LABEL org.opencontainers.image.licenses="MIT"
COPY --from=worker-build --chown=nextjs:nodejs /prod/worker ./worker
COPY --from=worker-build /usr/share/licenses/langfuse-native /usr/share/licenses/langfuse-native
RUN chmod +x ./worker/entrypoint.sh
RUN cis-harden
USER 1001
EXPOSE 3030
ENV PORT=3030
ENTRYPOINT ["dumb-init", "--", "./worker/entrypoint.sh"]
CMD ["node", "worker/dist/index.js"]
