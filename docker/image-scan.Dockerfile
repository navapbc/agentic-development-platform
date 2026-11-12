# syntax=docker/dockerfile:1

# The Amazon Inspector SBOM generator, which runs only on Linux, so the publish step runs it from
# this image on any host. It reads an image archive and writes the CycloneDX SBOM the Inspector
# Scan API evaluates.

ARG TARGETARCH

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS download-arm64
ADD --checksum=sha256:90229e9a8d35c24b3e4640347f2d0efd8af28d723abc2cc01d1226430190aa0f https://amazon-inspector-sbomgen.s3.amazonaws.com/1.17.5/linux/arm64/inspector-sbomgen.zip /tmp/sbomgen.zip

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS download-amd64
ADD --checksum=sha256:28bc3e374541469662988f3c7c1a37399716d3061204e127763dff4f9af99e47 https://amazon-inspector-sbomgen.s3.amazonaws.com/1.17.5/linux/amd64/inspector-sbomgen.zip /tmp/sbomgen.zip

FROM download-${TARGETARCH} AS unpack
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/sbomgen.zip').extractall('/tmp/sbomgen')" \
    && find /tmp/sbomgen -name inspector-sbomgen -type f -exec install -m 0555 {} /usr/local/bin/inspector-sbomgen \;

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
COPY --from=unpack /usr/local/bin/inspector-sbomgen /usr/local/bin/inspector-sbomgen
RUN inspector-sbomgen --version
# The publish step runs the image as the host's own user, so it reads and writes the work directory.
USER 10001
ENV HOME=/tmp
ENTRYPOINT ["inspector-sbomgen"]
