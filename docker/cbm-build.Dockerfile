# syntax=docker/dockerfile:1
#
# Build the codebase-memory-mcp linux binary from source.
# The build context is the CBM source checkout, not this repository:
#   docker build -f docker/cbm-build.Dockerfile --target export \
#     --output type=local,dest=vendor/bin <path-to-codebase-memory-mcp>
# Produces vendor/bin/codebase-memory-mcp for docker/cbm.Dockerfile to COPY.

FROM debian:trixie@sha256:34cd9e9fd437c0a095ec39cb2e73422c9f30821b0d0848ed74fd0d43bae4d958 AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc g++ make zlib1g-dev pkg-config python3 git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --create-home builder \
 && install -d -o builder -g builder /src
WORKDIR /src
COPY --chown=builder:builder . /src
USER builder
RUN make -f Makefile.cbm clean-c || true
RUN make -f Makefile.cbm cbm CC=gcc CXX=g++

FROM scratch AS export
COPY --from=build /src/build/c/codebase-memory-mcp /codebase-memory-mcp
