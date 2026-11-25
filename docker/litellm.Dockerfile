# syntax=docker/dockerfile:1

# The LiteLLM proxy on Amazon Linux 2023, installed from the release wheel and the dependencies
# docker/litellm/requirements.txt locks by hash. The wheel carries the admin UI and the Prisma
# schema, and the build generates the Prisma client, as the upstream image does.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85

FROM ${UV_IMAGE} AS uv

FROM ${AL2023_IMAGE} AS build
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" install python3.14 nodejs24 nodejs24-npm libsndfile findutils \
 && dnf clean all
COPY docker/litellm/requirements.txt /tmp/litellm-requirements.txt
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv venv --quiet --python /usr/bin/python3.14 /app/.venv \
 && VIRTUAL_ENV=/app/.venv uv pip sync --quiet --no-cache --require-hashes /tmp/litellm-requirements.txt
ENV PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN schema="$(python -c 'import litellm, os; print(os.path.join(os.path.dirname(litellm.__file__), "proxy", "schema.prisma"))')" \
 && cp "${schema}" /app/schema.prisma \
 && HOME=/opt/prisma XDG_CACHE_HOME=/opt/prisma/.cache PRISMA_BINARY_CACHE_DIR=/opt/prisma/binaries \
      prisma generate --schema=/app/schema.prisma \
 && find /app/.venv -type d -path "*/tornado/test" -prune -exec rm -rf {} +

FROM ${AL2023_IMAGE} AS rds-ca
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b
RUN mkdir /out \
 && curl -fsSLo /out/rds-global-bundle.pem "${RDS_CA_BUNDLE_URL}" \
 && echo "${RDS_CA_BUNDLE_SHA256}  /out/rds-global-bundle.pem" | sha256sum -c -

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="MIT"
# The Prisma client runs the openssl command to pick its query engine, and without it LiteLLM
# cannot open its database.
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install bash python3.14 nodejs24 libsndfile openssl tzdata shadow-utils \
 && groupadd --gid 10001 litellm \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/litellm --shell /sbin/nologin litellm \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
WORKDIR /app
ENV PATH=/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PRISMA_BINARY_CACHE_DIR=/opt/prisma/binaries \
    PRISMA_CLI_PATH=/opt/prisma/binaries/node_modules/.bin/prisma \
    PRISMA_CLI_QUERY_ENGINE_TYPE=binary \
    PRISMA_OFFLINE_MODE=true
COPY --from=build /app /app
COPY --from=build /opt/prisma /opt/prisma
COPY --chmod=0555 docker/litellm/prod_entrypoint.sh /app/docker/prod_entrypoint.sh
RUN chmod -R a+rX /opt/prisma
COPY --from=rds-ca --chmod=0444 /out/rds-global-bundle.pem /etc/ssl/rds-global-bundle.pem
EXPOSE 4000/tcp
RUN cis-harden
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:4000/health/liveliness"]
ENTRYPOINT ["docker/prod_entrypoint.sh"]
CMD ["--port", "4000"]
