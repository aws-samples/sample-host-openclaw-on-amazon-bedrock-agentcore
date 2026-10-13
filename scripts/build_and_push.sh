#!/usr/bin/env bash
# Build and push the ARM64 bridge image to an existing ECR repository.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_URI="${1:?Usage: build_and_push.sh ECR_IMAGE_URI REGION}"
REGION="${2:?Deployment region is required}"
REGISTRY="${IMAGE_URI%%/*}"

if [[ "$IMAGE_URI" == *@* || "$IMAGE_URI" != *:* ]]; then
  echo "ERROR: build requires a tagged ECR image URI." >&2
  exit 1
fi
if ! docker buildx inspect --bootstrap >/dev/null; then
  echo "ERROR: configure a Docker buildx builder with linux/arm64 support." >&2
  exit 1
fi
aws ecr get-login-password --region "$REGION" |
  docker login --username AWS --password-stdin "$REGISTRY"
docker buildx build --platform linux/arm64 --provenance=false --push \
  --tag "$IMAGE_URI" --file "$PROJECT_DIR/bridge/Dockerfile" "$PROJECT_DIR/bridge"
