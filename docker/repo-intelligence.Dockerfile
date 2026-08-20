# syntax=docker/dockerfile:1

FROM golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS go-tools
ENV GOFLAGS=-mod=mod GOBIN=/out
RUN mkdir -p /out \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt@v0.0.0-20260729135938-2cb19912a407 \
 && go install github.com/sourcegraph/zoekt/cmd/zoekt-index@v0.0.0-20260729135938-2cb19912a407

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-cli
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && curl -fsSLo scip.tar.gz "https://github.com/scip-code/scip/releases/download/v0.9.0/scip-linux-${TARGETARCH}.tar.gz" \
 && curl -fsSLo scip.sha256 "https://github.com/scip-code/scip/releases/download/v0.9.0/scip-linux-${TARGETARCH}.tar.gz.sha256" \
 && echo "$(cut -d' ' -f1 scip.sha256)  scip.tar.gz" | sha256sum -c - \
 && tar -xzf scip.tar.gz -C /tmp \
 && install -m 0755 /tmp/scip /usr/local/bin/scip

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS terraform-ls
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && fname="terraform-ls_0.39.0_linux_${TARGETARCH}.zip" \
 && curl -fsSLo "$fname" "https://releases.hashicorp.com/terraform-ls/0.39.0/$fname" \
 && curl -fsSLo SHA256SUMS "https://releases.hashicorp.com/terraform-ls/0.39.0/terraform-ls_0.39.0_SHA256SUMS" \
 && grep " $fname$" SHA256SUMS | sha256sum -c - \
 && unzip -q "$fname" -d /tmp \
 && install -m 0755 /tmp/terraform-ls /usr/local/bin/terraform-ls

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-java
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && ver=v0.13.1 \
 && curl -fsSLo scip-java "https://github.com/scip-code/scip-java/releases/download/${ver}/scip-java-${ver}" \
 && curl -fsSLo scip-java.sha256 "https://github.com/scip-code/scip-java/releases/download/${ver}/scip-java-${ver}.sha256" \
 && echo "$(cut -d' ' -f1 scip-java.sha256)  scip-java" | sha256sum -c - \
 && install -m 0755 /tmp/scip-java /usr/local/bin/scip-java

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS phpactor-cli
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && ver=2026.07.22.0 \
 && sha=8c0155380b9d7559a12f35ddf8d09c1dc23e72f1797498038251fc35ad15574d \
 && curl -fsSLo phpactor.phar "https://github.com/phpactor/phpactor/releases/download/${ver}/phpactor.phar" \
 && echo "${sha}  phpactor.phar" | sha256sum -c - \
 && install -m 0755 /tmp/phpactor.phar /usr/local/bin/phpactor

FROM debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258 AS scip-ruby
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && ver=scip-ruby-v0.4.7 \
 && sha=a068c7c3b2042b9eac563ce77ce35dcaca666b418530b1db9f932a3dbc7175dd \
 && curl -fsSLo scip-ruby "https://github.com/sourcegraph/scip-ruby/releases/download/${ver}/scip-ruby-x86_64-linux" \
 && echo "${sha}  scip-ruby" | sha256sum -c - \
 && install -m 0755 /tmp/scip-ruby /usr/local/bin/scip-ruby

FROM maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS java-tools

FROM node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    JAVA_HOME=/opt/java/openjdk \
    MAVEN_HOME=/opt/maven \
    PATH=/opt/venv/bin:/opt/java/openjdk/bin:/opt/maven/bin:/usr/local/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv git ca-certificates php-cli php-mbstring php-xml \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip

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
COPY pyproject.toml README.md ./
COPY services ./services
COPY adapters ./adapters
COPY schemas ./schemas
COPY database ./database
COPY scripts ./scripts

RUN pip install -e ".[dev]"

RUN useradd --create-home --uid 10001 appuser
USER appuser

CMD ["python", "-m", "services.repository_intelligence.server"]
