# syntax=docker/dockerfile:1

# The Amazon Inspector SBOM generator, which runs only on Linux, so the publish step runs it from
# this image on any host. It reads an image archive and writes the CycloneDX SBOM the Inspector
# Scan API evaluates.

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS unpack
ARG TARGETARCH
ARG SBOMGEN_VERSION=1.17.5
ARG SBOMGEN_SHA256_AMD64=28bc3e374541469662988f3c7c1a37399716d3061204e127763dff4f9af99e47
ARG SBOMGEN_SHA256_ARM64=90229e9a8d35c24b3e4640347f2d0efd8af28d723abc2cc01d1226430190aa0f
RUN case "${TARGETARCH}" in amd64) sum="${SBOMGEN_SHA256_AMD64}" ;; arm64) sum="${SBOMGEN_SHA256_ARM64}" ;; *) exit 1 ;; esac \
    && python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/sbomgen.zip')" \
         "https://amazon-inspector-sbomgen.s3.amazonaws.com/${SBOMGEN_VERSION}/linux/${TARGETARCH}/inspector-sbomgen.zip" \
    && echo "${sum}  /tmp/sbomgen.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/sbomgen.zip').extractall('/tmp/sbomgen')" \
    && find /tmp/sbomgen -name inspector-sbomgen -type f -exec install -m 0555 {} /usr/local/bin/inspector-sbomgen \;

FROM public.ecr.aws/docker/library/python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
COPY --from=unpack /usr/local/bin/inspector-sbomgen /usr/local/bin/inspector-sbomgen
RUN inspector-sbomgen --version
# The publish step runs the image as the host's own user, so it reads and writes the work directory.
USER 10001
ENV HOME=/tmp
HEALTHCHECK NONE
ENTRYPOINT ["inspector-sbomgen"]
