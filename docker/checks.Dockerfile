# syntax=docker/dockerfile:1

# The catalog checks images a checks pod runs a repository's checks command in, one target per
# runtime profile, each with its toolchain from the Amazon Linux packages. The command runs as
# root, the way the checks pod runs it in any image.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:75a2d4226d9b98c914d7eed878a03bef4f365ad548be5d28d464ccba504cec79

FROM ${AL2023_IMAGE} AS checks-base
ARG AL2023_RELEASE=2023.12.20261120
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install bash git-core findutils tar gzip unzip which diffutils procps-ng \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8
WORKDIR /workspace
HEALTHCHECK NONE

FROM checks-base AS checks-python
ARG AL2023_RELEASE=2023.12.20261120
LABEL org.opencontainers.image.licenses="PSF-2.0"
RUN dnf -y --releasever="${AL2023_RELEASE}" install python3.14 python3.14-pip \
 && dnf clean all \
 && rm -rf /var/cache/dnf \
 && ln -s /usr/bin/python3.14 /usr/local/bin/python3 \
 && ln -s /usr/bin/python3.14 /usr/local/bin/python \
 && ln -s /usr/bin/pip3.14 /usr/local/bin/pip3 \
 && ln -s /usr/bin/pip3.14 /usr/local/bin/pip
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore
RUN cis-harden

FROM checks-base AS checks-node
ARG AL2023_RELEASE=2023.12.20261120
LABEL org.opencontainers.image.licenses="MIT"
RUN dnf -y --releasever="${AL2023_RELEASE}" install nodejs24 nodejs24-npm \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV NPM_CONFIG_UPDATE_NOTIFIER=false
RUN cis-harden

FROM checks-base AS checks-jvm
ARG AL2023_RELEASE=2023.12.20261120
LABEL org.opencontainers.image.licenses="GPL-2.0-with-classpath-exception"
RUN dnf -y --releasever="${AL2023_RELEASE}" install java-21-amazon-corretto-devel maven-amazon-corretto21 \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV JAVA_HOME=/usr/lib/jvm/java-21-amazon-corretto
RUN cis-harden

FROM checks-base AS checks-go
ARG AL2023_RELEASE=2023.12.20261120
LABEL org.opencontainers.image.licenses="BSD-3-Clause"
# golang-bin recommends mercurial, which would add the system Python, and subversion. Go modules
# fetch with the git-core of checks-base.
RUN dnf -y --releasever="${AL2023_RELEASE}" --setopt=install_weak_deps=0 install golang \
 && dnf clean all \
 && rm -rf /var/cache/dnf
ENV GOTOOLCHAIN=local
RUN cis-harden
