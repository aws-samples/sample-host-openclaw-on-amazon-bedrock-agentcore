#!/usr/bin/env bash
# scripts/codebuild-push.sh — Build and push the bridge container via CodeBuild.
#
# Packages bridge/ into a zip, uploads it to S3, starts the CodeBuild project,
# and streams status until the build finishes. No local Docker required.
#
# Usage:
#   ./scripts/codebuild-push.sh           # uses image_version from cdk.json
#   ./scripts/codebuild-push.sh 36        # override image version

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo "us-west-2")}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
PROJECT_NAME="openclaw-bridge-build"
SOURCE_BUCKET="openclaw-build-source-${ACCOUNT}-${REGION}"

# Resolve image version: arg > cdk.json > fallback "1"
if [[ $# -ge 1 ]]; then
	IMAGE_VERSION="$1"
else
	IMAGE_VERSION=$(python3 -c "import json; d=json.load(open('cdk.json')); print(d['context'].get('image_version', 1))" 2>/dev/null || echo "1")
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BRIDGE_DIR="${REPO_ROOT}/bridge"

echo "==> Region:        ${REGION}"
echo "==> Account:       ${ACCOUNT}"
echo "==> Image version: v${IMAGE_VERSION}"
echo "==> Source bucket: s3://${SOURCE_BUCKET}/bridge-source.zip"
echo ""

# --- Package bridge/ source --------------------------------------------------
echo "==> Packaging bridge/ ..."
TMPZIP=$(mktemp "/tmp/bridge-source.XXXXXX.zip")
trap 'rm -f "$TMPZIP"' EXIT

(
	cd "$BRIDGE_DIR"
	zip -r "$TMPZIP" . \
		--exclude "*.DS_Store" \
		--exclude "*/.DS_Store" \
		--exclude "node_modules/*" \
		--exclude ".git/*" \
		--exclude "*.test.js" \
		>/dev/null
)
echo "    Packaged $(du -sh "$TMPZIP" | cut -f1) -> bridge-source.zip"

# --- Upload to S3 ------------------------------------------------------------
echo "==> Uploading to S3 ..."
aws s3 cp "$TMPZIP" "s3://${SOURCE_BUCKET}/bridge-source.zip" \
	--region "$REGION" \
	--no-progress
echo "    Upload complete."

# --- Start CodeBuild build ---------------------------------------------------
echo ""
echo "==> Starting CodeBuild build ..."
BUILD_ID=$(aws codebuild start-build \
	--project-name "$PROJECT_NAME" \
	--environment-variables-override \
	"name=IMAGE_VERSION,value=v${IMAGE_VERSION},type=PLAINTEXT" \
	--region "$REGION" \
	--query "build.id" --output text)

echo "    Build ID: ${BUILD_ID}"
echo ""
BUILD_URL="https://${REGION}.console.aws.amazon.com/codesuite/codebuild/${ACCOUNT}/projects/${PROJECT_NAME}/build/${BUILD_ID//:/\/}/"
echo "    Console:  ${BUILD_URL}"
echo ""

# --- Poll until complete -----------------------------------------------------
echo "==> Waiting for build to complete (polling every 20s) ..."
PREV_PHASE=""
while true; do
	BUILD_INFO=$(aws codebuild batch-get-builds \
		--ids "$BUILD_ID" \
		--region "$REGION" \
		--query "builds[0].{status:buildStatus,phase:currentPhase}" \
		--output json)

	STATUS=$(echo "$BUILD_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'])")
	PHASE=$(echo "$BUILD_INFO" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['phase'])")

	if [[ "$PHASE" != "$PREV_PHASE" ]]; then
		echo "    [$(date '+%H:%M:%S')] Phase: ${PHASE}"
		PREV_PHASE="$PHASE"
	fi

	case "$STATUS" in
	SUCCEEDED)
		echo ""
		echo "==> Build SUCCEEDED."
		echo "    Image pushed: ${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/openclaw-bridge:v${IMAGE_VERSION}"
		exit 0
		;;
	FAILED | FAULT | STOPPED | TIMED_OUT)
		echo ""
		echo "==> Build FAILED (status: ${STATUS})."
		echo "    Logs: ${BUILD_URL}"
		exit 1
		;;
	esac

	sleep 20
done
