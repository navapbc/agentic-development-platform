FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin engine

COPY bin /engine/bin
COPY policy.py rules.toml /engine/

USER engine
WORKDIR /workspace
HEALTHCHECK NONE
