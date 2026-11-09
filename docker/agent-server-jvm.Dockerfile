# Catalog sandbox image agent-server-jvm: the platform's agent-server image with JDK 21 and Maven,
# copied from the image the java-spring profile's checks run in, so the agent and the checks use
# the same toolchain. Gradle projects use their ./gradlew wrapper.
FROM public.ecr.aws/docker/library/maven:3.9.11-eclipse-temurin-21@sha256:6fdc855a6ed81d288ca7ca37ac6ff5e9308b612485c0801d70b25a858c83d237 AS java-tools

FROM ghcr.io/openhands/agent-server:1.56.0-python@sha256:718bf6d719f34184810ecaebe607f1aafaf526e109654815e61ddd6238728023
LABEL org.opencontainers.image.licenses="Apache-2.0"
ENV JAVA_HOME=/opt/java/openjdk \
    MAVEN_HOME=/opt/maven \
    PATH=/opt/java/openjdk/bin:/opt/maven/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY --from=java-tools /opt/java/openjdk /opt/java/openjdk
COPY --from=java-tools /usr/share/maven /opt/maven
USER openhands
HEALTHCHECK NONE
