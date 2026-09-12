#!/usr/bin/env bash
# Build and push the jumphost-base image to the account ECR repository the
# bedrock-jumphost-fargate module creates. Run this after the module is applied
# and before opening a session, and again whenever the tag is bumped.
set -euo pipefail

tag="v1"
arch="arm64"
repo="agentic-platform/jumphost-base"
base_image=""
region="${AWS_REGION:-us-east-1}"
context_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../docker/jumphost-base" && pwd)"

while getopts "t:a:r:b:R:" opt; do
  case "$opt" in
    t) tag="$OPTARG" ;;
    a) arch="$OPTARG" ;;
    r) repo="$OPTARG" ;;
    b) base_image="$OPTARG" ;;
    R) region="$OPTARG" ;;
    *) echo "usage: $0 [-t tag] [-a arm64|amd64] [-r repo] [-b base_image] [-R region]" >&2; exit 2 ;;
  esac
done

case "$arch" in
  arm64) platform="linux/arm64" ;;
  amd64) platform="linux/amd64" ;;
  *) echo "error: -a must be arm64 or amd64" >&2; exit 2 ;;
esac

account_id="$(aws sts get-caller-identity --query Account --output text)"
registry="${account_id}.dkr.ecr.${region}.amazonaws.com"
image="${registry}/${repo}:${tag}"

echo "[publish] target ${image} for ${platform}" >&2

aws ecr get-login-password --region "$region" | docker login --username AWS --password-stdin "$registry"

docker buildx build --platform "$platform" --provenance=false --sbom=false ${base_image:+--build-arg BASE_IMAGE="$base_image"} -t "$image" --push "$context_dir"

echo "[publish] pushed ${image}" >&2
