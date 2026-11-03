FROM ghcr.io/openhands/agent-server:latest-java
ENV MAVEN_OPTS="${MAVEN_OPTS} -Ddependency-check.skip=true -DskipITs=true -Dsurefire.excludes=**/*IT.java"
