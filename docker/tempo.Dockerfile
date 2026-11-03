# syntax=docker/dockerfile:1

FROM public.ecr.aws/docker/library/golang:1.27-trixie@sha256:6212da3924947f4b6a939df02ea627c13f338f1a41d6c3fcb0dd9d076eef46c4 AS healthprobe
WORKDIR /src
COPY docker/healthprobe/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/healthprobe .

FROM docker.io/grafana/tempo:2.10.8@sha256:f0561deb1c68ec44d6e6e7e4487f30106c4e5e768642077695b37958b105812a
COPY --from=healthprobe /out/healthprobe /usr/local/bin/healthprobe
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["/usr/local/bin/healthprobe", "http://127.0.0.1:3200/ready"]
