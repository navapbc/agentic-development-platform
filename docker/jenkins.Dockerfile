FROM jenkins/jenkins:lts-jdk17

ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false"

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-full python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*
USER jenkins

COPY docker/jenkins/plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY docker/jenkins/init.groovy.d/ /usr/share/jenkins/ref/init.groovy.d/
