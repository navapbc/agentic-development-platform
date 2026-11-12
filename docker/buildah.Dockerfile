# syntax=docker/dockerfile:1

# The builder the gate, checks, and scanner pods start from: buildah, plus notation and the AWS
# Signer plugin, which verify a signed image's signature immediately before the pod pulls it.
# The plugin is published only under a "latest" path, so its checksum pins the release, and a new
# release fails the build until the checksum is updated.

ARG BUILDAH_IMAGE=quay.io/buildah/stable:v1.43.5@sha256:7c2b605262748ace2c5c57c5550942d0adad57aec82ede3070927722306c0ffb
ARG NOTATION_VERSION=1.3.2
ARG NOTATION_SHA256_ARM64=34143c7b816a50104824278080a5cf12bbf855488e003f62e6acf27ed25c423c
ARG NOTATION_SHA256_AMD64=e1a0f060308086bf8020b2d31defb7c5348f133ca0dba6a1a7820ef3cbb6dfe5
ARG SIGNER_PLUGIN_SHA256_ARM64=b416954ac98a20d71f968c4f7cd7b13905328352f43298a405a8261016a8be51
ARG SIGNER_PLUGIN_SHA256_AMD64=f8efa548932edc4851f4ba464496994bad753cdf21cd32baa575f7291a26c7b9
ARG SIGNER_ROOT_SHA256=0ccf529561f610b9eb7f9b0ee02fffe2f74f7ffdf0ac56348ca224e879461c9b

FROM ${BUILDAH_IMAGE} AS tools-arm64
ARG NOTATION_VERSION
ARG NOTATION_SHA256_ARM64
ARG SIGNER_PLUGIN_SHA256_ARM64
ADD --checksum=sha256:${NOTATION_SHA256_ARM64} https://github.com/notaryproject/notation/releases/download/v${NOTATION_VERSION}/notation_${NOTATION_VERSION}_linux_arm64.tar.gz /tmp/notation.tar.gz
ADD --checksum=sha256:${SIGNER_PLUGIN_SHA256_ARM64} https://d2hvyiie56hcat.cloudfront.net/linux/arm64/plugin/latest/notation-aws-signer-plugin.zip /tmp/plugin.zip

FROM ${BUILDAH_IMAGE} AS tools-amd64
ARG NOTATION_VERSION
ARG NOTATION_SHA256_AMD64
ARG SIGNER_PLUGIN_SHA256_AMD64
ADD --checksum=sha256:${NOTATION_SHA256_AMD64} https://github.com/notaryproject/notation/releases/download/v${NOTATION_VERSION}/notation_${NOTATION_VERSION}_linux_amd64.tar.gz /tmp/notation.tar.gz
ADD --checksum=sha256:${SIGNER_PLUGIN_SHA256_AMD64} https://d2hvyiie56hcat.cloudfront.net/linux/amd64/plugin/latest/notation-aws-signer-plugin.zip /tmp/plugin.zip

FROM tools-${TARGETARCH} AS tools
ARG SIGNER_ROOT_SHA256
ADD --checksum=sha256:${SIGNER_ROOT_SHA256} https://d2hvyiie56hcat.cloudfront.net/aws-signer-notation-root.cert /tmp/aws-signer-notation-root.crt
RUN mkdir -p /out/bin /out/notation/plugins/com.amazonaws.signer.notation.plugin /out/notation/truststore/x509/signingAuthority/aws-signer-ts \
    && tar -xzf /tmp/notation.tar.gz -C /out/bin notation \
    && unzip -q /tmp/plugin.zip notation-com.amazonaws.signer.notation.plugin -d /out/notation/plugins/com.amazonaws.signer.notation.plugin \
    && cp /tmp/aws-signer-notation-root.crt /out/notation/truststore/x509/signingAuthority/aws-signer-ts/

FROM ${BUILDAH_IMAGE}
COPY --from=tools --chmod=0555 /out/bin/notation /usr/local/bin/notation
COPY --from=tools /out/notation /root/.config/notation
RUN chmod 0555 /root/.config/notation/plugins/com.amazonaws.signer.notation.plugin/notation-com.amazonaws.signer.notation.plugin \
    && notation version \
    && notation plugin list | grep -q com.amazonaws.signer.notation.plugin
