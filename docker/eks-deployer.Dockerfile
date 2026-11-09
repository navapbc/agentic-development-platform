# syntax=docker/dockerfile:1

ARG TARGETARCH

# The tools come from per-architecture stages, so each download is pinned by its own checksum.
FROM scratch AS tools-arm64
ADD --chmod=0555 --checksum=sha256:fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509 https://dl.k8s.io/release/v1.37.2/bin/linux/arm64/kubectl /kubectl
ADD --checksum=sha256:8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88 https://get.helm.sh/helm-v4.3.1-linux-arm64.tar.gz /helm.tar.gz
ADD --chmod=0555 --checksum=sha256:0e1790be798dbf4462ce552f660e02d6aa3373ad9642b6c54f9964eb981766c6 https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v0.7.24/aws-iam-authenticator_0.7.24_linux_arm64 /aws-iam-authenticator

FROM scratch AS tools-amd64
ADD --chmod=0555 --checksum=sha256:c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4 https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl /kubectl
ADD --checksum=sha256:0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz /helm.tar.gz
ADD --chmod=0555 --checksum=sha256:136e41012ee248d1949ec7b512ac37ec79830f2ab196484f8e650e29e57ad853 https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v0.7.24/aws-iam-authenticator_0.7.24_linux_amd64 /aws-iam-authenticator

FROM tools-${TARGETARCH} AS tools

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS unpack
COPY --from=tools /helm.tar.gz /tmp/helm.tar.gz
RUN mkdir /tmp/helm && tar -xzf /tmp/helm.tar.gz -C /tmp/helm --strip-components=1

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=tools /kubectl /aws-iam-authenticator /usr/local/bin/
COPY --from=unpack /tmp/helm/helm /usr/local/bin/helm
# ADD --chmod also applies its mode to the directories it creates, so they exist first.
RUN mkdir -p /opt/eks-deployer/charts
# The AWS Load Balancer Controller chart, vendored because DNS Firewall resolves no chart repository.
ADD --chmod=0444 --checksum=sha256:9255bf39873b6cb31106dbe61dc87c3617bff4197783b5b94381811a13f99c3c https://aws.github.io/eks-charts/aws-load-balancer-controller-3.6.2.tgz /opt/eks-deployer/charts/aws-load-balancer-controller.tgz

COPY LICENSE NOTICE /usr/share/doc/agentic-dev-platform/
COPY scripts/__init__.py scripts/eks_render.py scripts/tenant_render.py scripts/eks_deploy_task.py /opt/eks-deployer/scripts/
COPY services/__init__.py services/process.py /opt/eks-deployer/services/
COPY deploy/kubernetes /opt/eks-deployer/deploy/kubernetes
COPY openhands/policies /opt/eks-deployer/openhands/policies
COPY config/platform/litellm-config.yaml config/platform/litellm_redaction.py /opt/eks-deployer/config/platform/
COPY config/policies /opt/eks-deployer/config/policies

WORKDIR /opt/eks-deployer
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 EKS_DEPLOYER_ROOT=/opt/eks-deployer
USER 65534:65534
ENTRYPOINT ["python", "-m", "scripts.eks_deploy_task"]
CMD ["deploy"]
