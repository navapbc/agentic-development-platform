# syntax=docker/dockerfile:1

FROM public.ecr.aws/docker/library/golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS build
ENV CGO_ENABLED=0
WORKDIR /src/tls-sidecar
COPY docker/tls-sidecar/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/tls-sidecar .
WORKDIR /src/healthprobe
COPY docker/healthprobe/ ./
RUN go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM scratch
LABEL org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /out/tls-sidecar /out/healthprobe /usr/local/bin/
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/tls-sidecar"]
