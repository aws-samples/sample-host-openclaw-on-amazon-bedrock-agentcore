#!/usr/bin/env node
/**
 * delete_user_file — Delete a file from a user's S3-namespaced storage.
 * Usage: node delete.js <user_id> <filename>
 */
const { S3Client, DeleteObjectCommand } = require("@aws-sdk/client-s3");
const { BUCKET, REGION, buildKey, validateUserId, validateBucket } = require("./common");

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];

  validateUserId(userId);
  validateBucket();

  if (!filename) {
    console.error("Error: filename argument is required.");
    process.exit(1);
  }

  const key = buildKey(userId, filename);
  const client = new S3Client({ region: REGION });

  await client.send(new DeleteObjectCommand({
    Bucket: BUCKET,
    Key: key,
  }));

  console.log(`File deleted: ${key}`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
