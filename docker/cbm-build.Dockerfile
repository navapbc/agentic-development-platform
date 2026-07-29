# syntax=docker/dockerfile:1
#
# Build the codebase-memory-mcp linux binary from source.
# The build context is the CBM source checkout, not this repository:
#   docker build -f docker/cbm-build.Dockerfile --target export \
#     --output type=local,dest=vendor/bin <path-to-codebase-memory-mcp>
# Produces vendor/bin/codebase-memory-mcp for docker/cbm.Dockerfile to COPY.

FROM debian:bookworm AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc g++ make zlib1g-dev pkg-config python3 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . /src
RUN make -f Makefile.cbm clean-c || true
RUN make -f Makefile.cbm cbm CC=gcc CXX=g++

FROM scratch AS export
COPY --from=build /src/build/c/codebase-memory-mcp /codebase-memory-mcp
