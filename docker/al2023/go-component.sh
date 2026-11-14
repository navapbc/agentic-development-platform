#!/bin/sh
# Install one third-party Go command at /out/$NAME from its upstream release or built from its tag.
#
# MODE release downloads RELEASE_URL, verifies it against the SHA256 for TARGETARCH, and takes
# RELEASE_MEMBER from it when the release is a tar.gz or a zip. MODE source clones REPOSITORY at VERSION,
# raises each module in the comma-separated GO_GET to its fixed version, and builds PACKAGE with
# the image's Go toolchain. {version}, {v} (VERSION without its leading v), and {arch} expand in
# RELEASE_URL, RELEASE_MEMBER, and LDFLAGS. {release_arch} expands to the name ARCH_NAMES, such as
# "amd64=64bit,arm64=ARM64", gives TARGETARCH, for a project that names its assets its own way.
set -eu
: "${NAME:?}" "${MODE:?}" "${VERSION:?}" "${TARGETARCH:?}"

release_arch="${TARGETARCH}"
for pair in $(printf '%s' "${ARCH_NAMES:-}" | tr , ' '); do
    [ "${pair%%=*}" = "${TARGETARCH}" ] && release_arch="${pair#*=}"
done

expand() {
    printf '%s' "$1" | sed "s|{version}|${VERSION}|g; s|{v}|${VERSION#v}|g; s|{release_arch}|${release_arch}|g; s|{arch}|${TARGETARCH}|g"
}

mkdir -p /out
case "${MODE}" in
release)
    case "${TARGETARCH}" in
    amd64) sum="${SHA256_AMD64:?}" ;;
    arm64) sum="${SHA256_ARM64:?}" ;;
    *) echo "go-component: no release checksum for ${TARGETARCH}" >&2; exit 1 ;;
    esac
    curl -fsSLo /tmp/release "$(expand "${RELEASE_URL:?}")"
    echo "${sum}  /tmp/release" | sha256sum -c -
    if [ -n "${RELEASE_MEMBER:-}" ]; then
        mkdir -p /tmp/unpacked
        case "$(expand "${RELEASE_URL}")" in
        *.zip) unzip -q /tmp/release -d /tmp/unpacked ;;
        *) tar -xzf /tmp/release -C /tmp/unpacked ;;
        esac
        cp "/tmp/unpacked/$(expand "${RELEASE_MEMBER}")" "/out/${NAME}"
    else
        cp /tmp/release "/out/${NAME}"
    fi
    ;;
source)
    git clone --quiet --depth 1 --branch "${VERSION}" "${REPOSITORY:?}" /src
    cd "/src/${MODULE_DIR:-.}"
    if [ -n "${GO_GET:-}" ]; then
        # shellcheck disable=SC2046
        GOWORK=off GOFLAGS=-mod=mod go get $(printf '%s' "${GO_GET}" | tr , ' ')
    fi
    GOWORK=off GOFLAGS=-mod=mod go build -trimpath -ldflags="-s -w $(expand "${LDFLAGS:-}")" \
        -o "/out/${NAME}" "${PACKAGE:?}"
    ;;
*)
    echo "go-component: MODE must be release or source, not ${MODE}" >&2
    exit 1
    ;;
esac
chmod 0555 "/out/${NAME}"
