# syntax=docker/dockerfile:1

# ClickHouse for Langfuse on Amazon Linux 2023. The server binary, its configuration, and its
# entrypoint come from the pinned upstream release image, whose Ubuntu packages stay behind.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4
ARG CLICKHOUSE_IMAGE=docker.io/clickhouse/clickhouse-server:26.9.25.15@sha256:19bfacb135479a892551a8155cfbbc8211aedf2aa888cd91950b9ee7e76cc7e3

FROM ${CLICKHOUSE_IMAGE} AS upstream

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
LABEL org.opencontainers.image.licenses="Apache-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install bash grep findutils glibc-langpack-en wget shadow-utils \
 && groupadd --system --gid 101 clickhouse \
 && useradd --system --uid 101 --gid 101 --home-dir /nonexistent --shell /sbin/nologin clickhouse \
 && dnf -y remove shadow-utils \
 && dnf clean all \
 && rm -rf /var/cache/dnf
COPY --from=upstream /usr/bin/clickhouse /usr/bin/clickhouse
COPY --from=upstream /etc/clickhouse-server /etc/clickhouse-server
COPY --from=upstream /entrypoint.sh /entrypoint.sh
RUN for tool in server client keeper local format compressor extract-from-config; do \
      ln -s /usr/bin/clickhouse "/usr/bin/clickhouse-${tool}"; \
    done \
 && mkdir -p /var/lib/clickhouse /var/log/clickhouse-server /docker-entrypoint-initdb.d \
 && chown clickhouse:clickhouse /var/lib/clickhouse /var/log/clickhouse-server \
 && chmod 0777 /var/lib/clickhouse
RUN cis-harden
ENV LANG=en_US.UTF-8 \
    TZ=UTC \
    CLICKHOUSE_CONFIG=/etc/clickhouse-server/config.xml
EXPOSE 8123 9000 9009
VOLUME /var/lib/clickhouse
ENTRYPOINT ["/entrypoint.sh"]
