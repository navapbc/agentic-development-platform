# syntax=docker/dockerfile:1

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE} AS go
ARG AL2023_RELEASE=2023.12.20261112
ARG GO_VERSION=1.27.4
ARG GO_SHA256_AMD64=7226736c2339719cda28fbf3a54d71aa63bcefe376e98cef00034268dab24840
ARG GO_SHA256_ARM64=3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
ARG TARGETARCH
RUN dnf -y --releasever="${AL2023_RELEASE}" install tar gzip unzip git \
 && dnf clean all \
 && case "${TARGETARCH}" in amd64) sum="${GO_SHA256_AMD64}" ;; arm64) sum="${GO_SHA256_ARM64}" ;; *) exit 1 ;; esac \
 && curl -fsSLo /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${TARGETARCH}.tar.gz" \
 && echo "${sum}  /tmp/go.tgz" | sha256sum -c - \
 && tar -C /usr/local -xzf /tmp/go.tgz \
 && rm /tmp/go.tgz
ENV PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    CGO_ENABLED=0 \
    GOTOOLCHAIN=local
COPY --chmod=0555 docker/al2023/go-component.sh /usr/local/bin/go-component

FROM go AS kubectl
ARG KUBECTL_MODE=source
ARG KUBECTL_VERSION=v1.37.2
ARG KUBECTL_SHA256_AMD64=c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4
ARG KUBECTL_SHA256_ARM64=fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
ARG KUBECTL_GO_GET=golang.org/x/net@v0.62.0
ARG TARGETARCH
RUN NAME=kubectl \
    MODE="${KUBECTL_MODE}" \
    VERSION="${KUBECTL_VERSION}" \
    SHA256_AMD64="${KUBECTL_SHA256_AMD64}" \
    SHA256_ARM64="${KUBECTL_SHA256_ARM64}" \
    GO_GET="${KUBECTL_GO_GET}" \
    RELEASE_URL='https://dl.k8s.io/release/{version}/bin/linux/{arch}/kubectl' \
    REPOSITORY=https://github.com/kubernetes/kubernetes \
    PACKAGE=./cmd/kubectl \
    LDFLAGS='-X k8s.io/component-base/version.gitVersion={version} -X k8s.io/client-go/pkg/version.gitVersion={version}' \
    go-component

FROM go AS helm
ARG HELM_MODE=source
ARG HELM_VERSION=v4.3.1
ARG HELM_SHA256_AMD64=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
ARG HELM_SHA256_ARM64=8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88
ARG HELM_GO_GET=golang.org/x/net@v0.62.0
ARG TARGETARCH
RUN NAME=helm \
    MODE="${HELM_MODE}" \
    VERSION="${HELM_VERSION}" \
    SHA256_AMD64="${HELM_SHA256_AMD64}" \
    SHA256_ARM64="${HELM_SHA256_ARM64}" \
    GO_GET="${HELM_GO_GET}" \
    RELEASE_URL='https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz' \
    RELEASE_MEMBER='linux-{arch}/helm' \
    REPOSITORY=https://github.com/helm/helm \
    PACKAGE=./cmd/helm \
    LDFLAGS='-X helm.sh/helm/v4/internal/version.version={version}' \
    go-component

FROM go AS aws-iam-authenticator
ARG AWS_IAM_AUTHENTICATOR_MODE=source
ARG AWS_IAM_AUTHENTICATOR_VERSION=v0.7.24
ARG AWS_IAM_AUTHENTICATOR_SHA256_AMD64=136e41012ee248d1949ec7b512ac37ec79830f2ab196484f8e650e29e57ad853
ARG AWS_IAM_AUTHENTICATOR_SHA256_ARM64=0e1790be798dbf4462ce552f660e02d6aa3373ad9642b6c54f9964eb981766c6
ARG AWS_IAM_AUTHENTICATOR_GO_GET=golang.org/x/net@v0.62.0
ARG TARGETARCH
RUN NAME=aws-iam-authenticator \
    MODE="${AWS_IAM_AUTHENTICATOR_MODE}" \
    VERSION="${AWS_IAM_AUTHENTICATOR_VERSION}" \
    SHA256_AMD64="${AWS_IAM_AUTHENTICATOR_SHA256_AMD64}" \
    SHA256_ARM64="${AWS_IAM_AUTHENTICATOR_SHA256_ARM64}" \
    GO_GET="${AWS_IAM_AUTHENTICATOR_GO_GET}" \
    RELEASE_URL='https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/{version}/aws-iam-authenticator_{v}_linux_{arch}' \
    REPOSITORY=https://github.com/kubernetes-sigs/aws-iam-authenticator \
    PACKAGE=./cmd/aws-iam-authenticator \
    LDFLAGS='-X sigs.k8s.io/aws-iam-authenticator/pkg.Version={v}' \
    go-component

FROM ${AL2023_IMAGE}
ARG AL2023_RELEASE=2023.12.20261112
RUN dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && dnf -y --releasever="${AL2023_RELEASE}" install python3.12 \
 && dnf clean all \
 && rm -rf /var/cache/dnf \
 && ln -s /usr/bin/python3.12 /usr/local/bin/python

LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=kubectl /out/kubectl /usr/local/bin/kubectl
COPY --from=helm /out/helm /usr/local/bin/helm
COPY --from=aws-iam-authenticator /out/aws-iam-authenticator /usr/local/bin/aws-iam-authenticator
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
RUN cis-harden
USER 65534:65534
ENTRYPOINT ["python", "-m", "scripts.eks_deploy_task"]
CMD ["deploy"]
