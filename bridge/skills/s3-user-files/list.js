#!/usr/bin/env node
/**
 * list_user_files — List files in a user's S3-namespaced storage.
 * Usage: node list.js <user_id>
 */
const { S3Client, ListObjectsV2Command } = require("@aws-sdk/client-s3");
const {
  BUCKET,
  REGION,
  buildKey,
  validateUserId,
  validateBucket,
} = require("./common");

async function main() {
  const userId = process.argv[2];

  validateUserId(userId);
  validateBucket();

  const prefix = buildKey(userId);
  const client = new S3Client({ region: REGION });

  const response = await client.send(
    new ListObjectsV2Command({
      Bucket: BUCKET,
      Prefix: prefix,
    }),
  );

  const files = (response.Contents || []).map((obj) => {
    const name = obj.Key.replace(prefix, "");
    const sizeKB = (obj.Size / 1024).toFixed(1);
    const modified = obj.LastModified.toISOString().split("T")[0];
    return `- ${name} (${sizeKB} KB, ${modified})`;
  });

  if (files.length === 0) {
    console.log("No files stored for this user.");
  } else {
    let output = `Files for ${userId}:\n${files.join("\n")}`;
    if (response.IsTruncated) {
      output += `\n(truncated — more than ${files.length} files exist)`;
    }
    console.log(output);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
