#!/usr/bin/env node
/**
 * presign_user_file — Generate a SigV4 presigned URL for a user's S3 file.
 * Usage: node presign.js <user_id> <filename> [expires_in_seconds]
 *
 * AWS SDK v3 uses SigV4 by default, which is required for SSE-KMS encrypted buckets.
 */
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3");
const { getSignedUrl } = require("@aws-sdk/s3-request-presigner");
const { BUCKET, REGION, buildKey, validateUserId, validateBucket } = require("./common");

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];
  const expiresIn = parseInt(process.argv[4], 10) || 3600;

  validateUserId(userId);
  validateBucket();

  if (!filename) {
    console.error("Error: filename argument is required.");
    process.exit(1);
  }

  const MAX_EXPIRES = 7 * 24 * 3600; // 7 days (S3 max for IAM user; STS creds may be shorter)
  if (expiresIn < 1 || expiresIn > MAX_EXPIRES) {
    console.error(`Error: expires_in must be between 1 and ${MAX_EXPIRES} seconds.`);
    process.exit(1);
  }

  const key = buildKey(userId, filename);
  const client = new S3Client({ region: REGION });

  const url = await getSignedUrl(
    client,
    new GetObjectCommand({ Bucket: BUCKET, Key: key }),
    { expiresIn },
  );

  console.log(url);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
