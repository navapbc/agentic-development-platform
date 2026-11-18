# syntax=docker/dockerfile:1

# The hardened Amazon Linux 2023 base every platform image starts from. The base stage applies the
# ComplianceAsCode remediation of the CIS Amazon Linux 2023 Benchmark Level 2 Server profile to
# the minimal image and keeps it as cis-harden, which each image runs again after its own installs. The openscap stage builds OpenSCAP from its release tarball with the same
# content, and scans an image's root file system without the scanner entering that image. The
# verify stage scans the base and writes the results, and fails on a rule that neither passes nor
# appears in cis-accepted.txt.

ARG AL2023_IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:158a9044f9dc0777f098b76c8b54346f6ed4560b9a6a416dec7a5ba3bc483af4

FROM ${AL2023_IMAGE} AS content
ARG AL2023_RELEASE=2023.12.20261112
ARG SSG_VERSION=0.1.82
ARG SSG_SHA512=1caea418f0a5aaef7025e1655ca45a80942ea87ee832b943644ba6f9991b14a6ac5b35dddcd04b754e1fc8fbdee7b7f394507d24b123e821cca0354dc4e03cfd
RUN dnf -y --releasever="${AL2023_RELEASE}" install unzip \
 && dnf clean all \
 && curl -fsSLo /tmp/ssg.zip "https://github.com/ComplianceAsCode/content/releases/download/v${SSG_VERSION}/scap-security-guide-${SSG_VERSION}.zip" \
 && echo "${SSG_SHA512}  /tmp/ssg.zip" | sha512sum -c - \
 && unzip -q -j /tmp/ssg.zip \
      "scap-security-guide-${SSG_VERSION}/ssg-al2023-ds.xml" \
      "scap-security-guide-${SSG_VERSION}/bash/al2023-script-cis.sh" -d /content \
 && rm /tmp/ssg.zip

FROM ${AL2023_IMAGE} AS openscap
ARG AL2023_RELEASE=2023.12.20261112
ARG OPENSCAP_VERSION=1.4.6
ARG OPENSCAP_SHA512=4f4008ab6505bc4f4a68d2c15611062f3c41762f5dd37a02fcf1f37a5415776c0d52b4f32b3289adb4b22d8f82a8921fc59362a3cd130905fb70cd68dea96325
RUN dnf -y --releasever="${AL2023_RELEASE}" install gcc gcc-c++ make cmake tar gzip \
      pcre2-devel libxml2-devel libxslt-devel xmlsec1-devel xmlsec1-openssl-devel openssl-devel \
      zlib-devel bzip2-devel rpm-devel libacl-devel libcap-devel libblkid-devel libselinux-devel \
      libcurl-devel popt-devel procps-ng-devel libyaml-devel libgcrypt-devel \
 && curl -fsSLo /tmp/openscap.tar.gz "https://github.com/OpenSCAP/openscap/releases/download/${OPENSCAP_VERSION}/openscap-${OPENSCAP_VERSION}.tar.gz" \
 && echo "${OPENSCAP_SHA512}  /tmp/openscap.tar.gz" | sha512sum -c - \
 && mkdir /tmp/openscap \
 && tar -xzf /tmp/openscap.tar.gz -C /tmp/openscap --strip-components=1 \
 && cmake -S /tmp/openscap -B /tmp/openscap/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DENABLE_TESTS=OFF -DENABLE_DOCS=OFF -DENABLE_PYTHON3=OFF -DENABLE_PERL=OFF \
      -DENABLE_OSCAP_UTIL_DOCKER=OFF -DENABLE_OSCAP_UTIL_PODMAN=OFF -DENABLE_OSCAP_UTIL_VM=OFF \
      -DENABLE_OSCAP_UTIL_SSH=OFF -DENABLE_OSCAP_UTIL_AS_RPM=OFF -DENABLE_OSCAP_UTIL_IM=OFF \
      -DENABLE_OSCAP_UTIL_AUTOTAILOR=OFF \
 && cmake --build /tmp/openscap/build --parallel \
 && cmake --install /tmp/openscap/build \
 && rm -rf /tmp/openscap /tmp/openscap.tar.gz \
 && dnf clean all \
 && echo /usr/local/lib64 > /etc/ld.so.conf.d/usr-local-lib64.conf \
 && ldconfig
COPY --from=content /content /usr/local/share/cis
COPY --chmod=0555 docker/al2023/cis-scan.sh /usr/local/bin/cis-scan
COPY docker/al2023/cis-accepted.txt /usr/local/share/cis/cis-accepted.txt

FROM ${AL2023_IMAGE} AS base
ARG AL2023_RELEASE=2023.12.20261112
COPY --from=content /content/al2023-script-cis.sh /usr/local/share/cis/al2023-script-cis.sh
COPY --chmod=0555 docker/al2023/cis-harden.sh /usr/local/sbin/cis-harden
RUN echo "${AL2023_RELEASE}" > /usr/local/share/cis/releasever \
 && dnf -y --releasever="${AL2023_RELEASE}" upgrade \
 && cis-harden

FROM openscap AS verify
RUN --mount=from=base,target=/target,readwrite \
    cis-scan /target /evidence

FROM scratch AS evidence
COPY --from=verify /evidence /
