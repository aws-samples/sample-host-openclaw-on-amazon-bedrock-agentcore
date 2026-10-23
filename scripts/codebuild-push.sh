#!/usr/bin/env bash
# scripts/codebuild-push.sh — Build and push the bridge container via CodeBuild.
#
# Packages bridge/ into a zip, uploads it to the OpenClawBuild source bucket,
# starts the CodeBuild project and polls until the build finishes. No local
# Docker required. Progress goes to stderr; on success the tagged image URI is
# the only line on stdout, ready for scripts/deploy.sh:
#
#   IMAGE_URI=$(./scripts/codebuild-push.sh)
#   BUILD_MODE=codebuild ./scripts/deploy.sh --image-uri "$IMAGE_URI"
#
# Usage:
#   ./scripts/codebuild-push.sh           # tag v<image_version> from cdk.json
#   ./scripts/codebuild-push.sh 71        # override the image version (tag v71)
#
# Requires: the OpenClawBuild stack (cdk deploy OpenClawBuild -c enable_codebuild=true),
# uv, zip, the AWS CLI, and .venv-deploy (same setup as scripts/deploy.sh).
# ECR_REPOSITORY (default openclaw-bridge) must match the repository the stack
# was deployed for.
# OPENCLAW_ENV_SUFFIX (optional, as in the CDK app) selects OpenClawBuild-<suffix>,
# the stack `cdk deploy` creates with the same suffix.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEPLOY_PYTHON="${DEPLOY_PYTHON:-$PROJECT_DIR/.venv-deploy/bin/python}"
REPOSITORY="${ECR_REPOSITORY:-openclaw-bridge}"

log() { echo "$@" >&2; }
die() {
	echo "ERROR: $*" >&2
	exit 1
}

if [[ $# -gt 1 || "${1:-}" == -* ]]; then
	die "usage: $0 [IMAGE_VERSION]"
fi

cd "$PROJECT_DIR"
for tool in uv zip aws; do
	command -v "$tool" >/dev/null || die "install $tool before running $0."
done
if [ ! -x "$DEPLOY_PYTHON" ]; then
	log "ERROR: deployment Python is not executable: $DEPLOY_PYTHON"
	log "Setup: uv venv .venv-deploy"
	log "       uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt"
	exit 1
fi
PYTHON=(uv run --no-project --python "$DEPLOY_PYTHON" python)

# Stack name with the optional environment suffix; same rules as the CDK app
# (scripts/env_suffix.py). Empty keeps OpenClawBuild.
BUILD_STACK=$("${PYTHON[@]}" -c 'import sys; sys.path.insert(0, "scripts"); from env_suffix import with_suffix; print(with_suffix("OpenClawBuild"))') ||
	die "OPENCLAW_ENV_SUFFIX must be at most 12 lowercase letters, digits and single hyphens."

# Region and account resolve the same way as scripts/deploy.sh.
REGION="${CDK_DEFAULT_REGION:-$("${PYTHON[@]}" -c 'import json; print(json.load(open("cdk.json"))["context"].get("region", ""))')}"
REGION="${REGION:-$(aws configure get region || true)}"
ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
if [ -z "$REGION" ] || [ -z "$ACCOUNT" ]; then
	die "set CDK_DEFAULT_REGION and CDK_DEFAULT_ACCOUNT or configure AWS credentials."
fi

# Image version: arg > cdk.json image_version. The tag is v<version>, as in deploy.sh.
IMAGE_VERSION="${1:-$("${PYTHON[@]}" -c 'import json; print(json.load(open("cdk.json"))["context"].get("image_version", "1"))')}"
if [[ ! "$IMAGE_VERSION" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}$ ]]; then
	die "image version must be a valid ECR tag suffix, got '$IMAGE_VERSION'."
fi
IMAGE_TAG="v${IMAGE_VERSION}"

stack_output() {
	aws cloudformation describe-stacks --stack-name "$BUILD_STACK" --region "$REGION" \
		--query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
PROJECT_NAME=$(stack_output BuildProjectName)
SOURCE_BUCKET=$(stack_output BuildSourceBucketName)
STACK_REPOSITORY=$(stack_output EcrRepositoryName)
for value in "$PROJECT_NAME" "$SOURCE_BUCKET" "$STACK_REPOSITORY"; do
	if [ -z "$value" ] || [ "$value" = "None" ]; then
		die "read $BUILD_STACK outputs in $REGION; deploy it with: cdk deploy $BUILD_STACK -c enable_codebuild=true"
	fi
done
if [ "$STACK_REPOSITORY" != "$REPOSITORY" ]; then
	die "$BUILD_STACK pushes to '$STACK_REPOSITORY' but ECR_REPOSITORY is '$REPOSITORY'; redeploy the stack with the same ECR_REPOSITORY."
fi

# Same repository resolution as deploy.sh local-build (creates it on first use).
REPOSITORY_URI=$("${PYTHON[@]}" "$SCRIPT_DIR/runtime_image.py" --region "$REGION" --name "$REPOSITORY")
IMAGE_URI="${REPOSITORY_URI}:${IMAGE_TAG}"

log "==> Region:        ${REGION}"
log "==> Image:         ${IMAGE_URI}"
log "==> Source bucket: s3://${SOURCE_BUCKET}/bridge-source.zip"
log ""

# --- Package bridge/ source --------------------------------------------------
# Test files stay in: the Dockerfile copies the patch-openclaw*.test.js files.
log "==> Packaging bridge/ ..."
TMPZIP=$(mktemp "${TMPDIR:-/tmp}/bridge-source.XXXXXX")
trap 'rm -f "$TMPZIP" "$TMPZIP.zip"' EXIT
(
	cd "$PROJECT_DIR/bridge"
	zip -q -r "$TMPZIP.zip" . \
		--exclude "*.DS_Store" \
		--exclude "node_modules/*" \
		--exclude "*/node_modules/*" \
		--exclude ".git/*"
)
log "    Packaged $(du -sh "$TMPZIP.zip" | cut -f1) -> bridge-source.zip"

# --- Upload to S3 ------------------------------------------------------------
# put-object returns the VersionId of exactly the object this run wrote, and
# the build below is pinned to it, so overwriting bridge-source.zip after the
# upload cannot change what this build consumes. stacks/build_stack.py makes
# the bucket versioned; a bucket from an older OpenClawBuild deploy returns no
# VersionId and the build falls back to the latest object, as before.
log "==> Uploading to S3 ..."
SOURCE_VERSION=$(aws s3api put-object \
	--bucket "$SOURCE_BUCKET" --key bridge-source.zip \
	--body "$TMPZIP.zip" \
	--region "$REGION" \
	--query VersionId --output text)
SOURCE_VERSION_ARGS=()
if [[ "$SOURCE_VERSION" =~ ^[A-Za-z0-9._-]{1,1024}$ && "$SOURCE_VERSION" != "None" && "$SOURCE_VERSION" != "null" ]]; then
	log "    Source version: ${SOURCE_VERSION}"
	SOURCE_VERSION_ARGS=(--source-version "$SOURCE_VERSION")
else
	log "    WARNING: s3://${SOURCE_BUCKET} is not versioned, so the build uses the latest"
	log "    bridge-source.zip. Redeploy ${BUILD_STACK} to pin each build to its upload."
fi

# --- Start CodeBuild build ---------------------------------------------------
log "==> Starting CodeBuild build ..."
BUILD_ID=$(aws codebuild start-build \
	--project-name "$PROJECT_NAME" \
	${SOURCE_VERSION_ARGS[@]+"${SOURCE_VERSION_ARGS[@]}"} \
	--environment-variables-override "name=IMAGE_TAG,value=${IMAGE_TAG},type=PLAINTEXT" \
	--region "$REGION" \
	--query "build.id" --output text)
log "    Build ID: ${BUILD_ID}"
log "    Console:  https://${REGION}.console.aws.amazon.com/codesuite/codebuild/${ACCOUNT}/projects/${PROJECT_NAME}/build/${BUILD_ID/:/%3A}/?region=${REGION}"

# --- Poll until complete -----------------------------------------------------
log "==> Waiting for build to complete (polling every 20s) ..."
PREV_PHASE=""
while true; do
	BUILD_INFO=$(aws codebuild batch-get-builds \
		--ids "$BUILD_ID" --region "$REGION" \
		--query "builds[0].[buildStatus,currentPhase]" --output text)
	read -r STATUS PHASE <<<"$BUILD_INFO"
	[ -n "${STATUS:-}" ] || die "could not read the status of build ${BUILD_ID}."
	if [[ "$PHASE" != "$PREV_PHASE" ]]; then
		log "    [$(date '+%H:%M:%S')] Phase: ${PHASE}"
		PREV_PHASE="$PHASE"
	fi
	case "$STATUS" in
	SUCCEEDED)
		# The tag is mutable: deploy.sh pins whatever it points to at deploy
		# time. The digest form pins the image this build pushed.
		IMAGE_DIGEST=$(aws ecr describe-images \
			--repository-name "$REPOSITORY" --image-ids "imageTag=${IMAGE_TAG}" \
			--region "$REGION" --query 'imageDetails[0].imageDigest' --output text || true)
		log "==> Build SUCCEEDED. Deploy with:"
		if [[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
			log "    BUILD_MODE=codebuild ./scripts/deploy.sh --image-uri ${REPOSITORY_URI}@${IMAGE_DIGEST}"
			log "    (or --image-uri ${IMAGE_URI} to deploy whatever ${IMAGE_TAG} points to at deploy time)"
		else
			log "    BUILD_MODE=codebuild ./scripts/deploy.sh --image-uri ${IMAGE_URI}"
		fi
		echo "$IMAGE_URI"
		exit 0
		;;
	FAILED | FAULT | STOPPED | TIMED_OUT)
		die "build ${BUILD_ID} ended with status ${STATUS}; see the console link above."
		;;
	esac
	sleep 20
done
