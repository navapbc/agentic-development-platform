# syntax=docker/dockerfile:1

FROM public.ecr.aws/docker/library/golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS go-tools
ENV GOFLAGS=-mod=mod GOBIN=/out
RUN mkdir -p /out \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt@v0.0.0-20260729135938-2cb19912a407 \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt-index@v0.0.0-20260729135938-2cb19912a407

FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-cli
ARG TARGETARCH
WORKDIR /tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && case "${TARGETARCH}" in \
      amd64) sha=fc2e7273e110be9f35924da1066000183791e8bfdb0391355de6eaaa070fec75 ;; \
      arm64) sha=e97aaf597ab6a6b6fbb1bbc43993e6efb596284aaea9c3fa5b20d75186367c5e ;; \
      *) echo "scip has no pinned digest for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSLo scip.tar.gz "https://github.com/scip-code/scip/releases/download/v0.9.0/scip-linux-${TARGETARCH}.tar.gz" \
 && echo "${sha}  scip.tar.gz" | sha256sum -c - \
 && tar -xzf scip.tar.gz -C /tmp \
 && install -m 0755 /tmp/scip /usr/local/bin/scip

FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS terraform-ls
ARG TARGETARCH
WORKDIR /tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/* \
 && case "${TARGETARCH}" in \
      amd64) sha=7750edc736845fd8c04ff0fc6332423c12d8275b358668c8c17e8aedc43ef971 ;; \
      arm64) sha=62f32ea22cb78e5e5667ed638ad6e0fbde30ab59228d073c3c9bb249f89c7f5a ;; \
      *) echo "terraform-ls has no pinned digest for ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && fname="terraform-ls_0.39.0_linux_${TARGETARCH}.zip" \
 && curl -fsSLo "$fname" "https://releases.hashicorp.com/terraform-ls/0.39.0/$fname" \
 && echo "${sha}  ${fname}" | sha256sum -c - \
 && unzip -q "$fname" -d /tmp \
 && install -m 0755 /tmp/terraform-ls /usr/local/bin/terraform-ls

FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-java
WORKDIR /tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ver=v0.13.1 \
 && sha=a694cae143c32c5b6226362fb4bd268a8d13d3cd9b482819b3b0029a9a97b8fe \
 && curl -fsSLo scip-java "https://github.com/scip-code/scip-java/releases/download/${ver}/scip-java-${ver}" \
 && echo "${sha}  scip-java" | sha256sum -c - \
 && install -m 0755 /tmp/scip-java /usr/local/bin/scip-java

FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS phpactor-cli
WORKDIR /tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ver=2026.07.22.0 \
 && sha=8c0155380b9d7559a12f35ddf8d09c1dc23e72f1797498038251fc35ad15574d \
 && curl -fsSLo phpactor.phar "https://github.com/phpactor/phpactor/releases/download/${ver}/phpactor.phar" \
 && echo "${sha}  phpactor.phar" | sha256sum -c - \
 && install -m 0755 /tmp/phpactor.phar /usr/local/bin/phpactor

FROM public.ecr.aws/docker/library/debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-ruby
WORKDIR /tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && ver=scip-ruby-v0.4.7 \
 && sha=a068c7c3b2042b9eac563ce77ce35dcaca666b418530b1db9f932a3dbc7175dd \
 && curl -fsSLo scip-ruby "https://github.com/sourcegraph/scip-ruby/releases/download/${ver}/scip-ruby-x86_64-linux" \
 && echo "${sha}  scip-ruby" | sha256sum -c - \
 && install -m 0755 /tmp/scip-ruby /usr/local/bin/scip-ruby

FROM public.ecr.aws/docker/library/maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS java-tools

FROM public.ecr.aws/docker/library/node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d AS runtime
LABEL org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BIND_HOST=0.0.0.0 \
    VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    JAVA_HOME=/opt/java/openjdk \
    MAVEN_HOME=/opt/maven \
    PATH=/opt/venv/bin:/opt/java/openjdk/bin:/opt/maven/bin:/usr/local/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates php-cli php-mbstring php-xml \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @ast-grep/cli@0.45.0 @sourcegraph/scip-python@0.6.6 @sourcegraph/scip-typescript@0.3.14

COPY --from=go-tools /out/zoekt /out/zoekt-index /usr/local/bin/
COPY --from=scip-cli /usr/local/bin/scip /usr/local/bin/scip
COPY --from=terraform-ls /usr/local/bin/terraform-ls /usr/local/bin/terraform-ls
COPY --from=java-tools /opt/java/openjdk /opt/java/openjdk
COPY --from=java-tools /usr/share/maven /opt/maven
COPY --from=scip-java /usr/local/bin/scip-java /usr/local/bin/scip-java
COPY --from=phpactor-cli /usr/local/bin/phpactor /usr/local/bin/phpactor
COPY --from=scip-ruby /usr/local/bin/scip-ruby /usr/local/bin/scip-ruby

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.3@sha256:bee0a2a458fd956c5df7b8b4c5fed2257b0af34866e3da7b51db0be3acb375d1 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts

RUN uv sync --locked --extra dev

RUN useradd --create-home --uid 10001 appuser
RUN mkdir -p /app/repository_indexes && chown -R 10001:10001 /app/repository_indexes
USER appuser

CMD ["python", "-m", "services.repository_intelligence.server"]
