# syntax=docker/dockerfile:1

ARG TEMPORAL_ADMIN_TOOLS_IMAGE=docker.io/temporalio/admin-tools:1.32.1@sha256:9ef0dc5b063bf4ffd5c1512de72e0e73e225bd2c7006a18ce69bd592a8862a83
ARG RDS_CA_BUNDLE_URL=https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
ARG RDS_CA_BUNDLE_SHA256=90a6f1a7978a1b1b096dca6c57511ca48d3b8cf052f8e704baf358f34ea2f21b

FROM ${TEMPORAL_ADMIN_TOOLS_IMAGE}
ARG RDS_CA_BUNDLE_URL
ARG RDS_CA_BUNDLE_SHA256
ADD --checksum=sha256:${RDS_CA_BUNDLE_SHA256} --chmod=0444 ${RDS_CA_BUNDLE_URL} /etc/ssl/rds-global-bundle.pem
COPY --chmod=0555 docker/temporal-admin-tools.sh /usr/local/libexec/temporal-admin-tools.sh
USER temporal
ENTRYPOINT ["/bin/sh", "/usr/local/libexec/temporal-admin-tools.sh"]
CMD ["schema"]
