# Catalog sandbox image agent-server-go: the platform's agent-server image with the Go toolchain,
# copied from the image the go-service profile's checks run in, so the agent and the checks use
# the same Go release.
FROM public.ecr.aws/docker/library/golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS go-tools

FROM ghcr.io/openhands/agent-server:1.56.0-python@sha256:718bf6d719f34184810ecaebe607f1aafaf526e109654815e61ddd6238728023
LABEL org.opencontainers.image.licenses="Apache-2.0"
ENV GOROOT=/usr/local/go \
    GOPATH=/home/openhands/go \
    GOTOOLCHAIN=local \
    PATH=/usr/local/go/bin:/home/openhands/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY --from=go-tools /usr/local/go /usr/local/go
USER openhands
HEALTHCHECK NONE
