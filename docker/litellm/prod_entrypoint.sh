#!/bin/sh
# The upstream image's entrypoint path, which deploy/kubernetes/aws/kustomization.yaml execs.
exec litellm "$@"
