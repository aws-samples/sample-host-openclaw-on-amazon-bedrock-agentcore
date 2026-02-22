#!/usr/bin/env node
/**
 * read_user_file — Read a file from a user's S3-namespaced storage.
 * Usage: node read.js <user_id> <filename>
 */
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3");
const { BUCKET, REGION, buildKey, validateUserId, validateBucket, streamToString } = require("./common");

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

  try {
    const response = await client.send(new GetObjectCommand({
      Bucket: BUCKET,
      Key: key,
    }));
    const content = await streamToString(response.Body);
    console.log(content);
  } catch (err) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404) {
      console.log(`File not found: ${key}`);
    } else {
      throw err;
    }
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
