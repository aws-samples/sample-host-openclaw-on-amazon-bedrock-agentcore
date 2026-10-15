#!/usr/bin/env node
/**
 * download_user_file — Copy a file from a user's S3-namespaced storage to the
 * local sandbox as raw bytes (for ZIP archives and other binary files).
 * Usage: node download.js <user_id> <filename> [output_dir]
 *
 * Writes to /tmp/openclaw-uploads/<user_id>/<name> unless an absolute
 * output_dir is given, and prints the local path. Refuses objects larger than
 * 25 MB. Does not extract archives.
 */
const fs = require("node:fs");
const path = require("node:path");
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3");
const {
  BUCKET,
  REGION,
  MAX_DOWNLOAD_BYTES,
  buildKey,
  downloadPath,
  validateUserId,
  validateBucket,
} = require("./common");

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];
  const outDir = process.argv[4];

  validateUserId(userId);
  validateBucket();

  if (!filename) {
    console.error("Error: filename argument is required.");
    process.exit(1);
  }

  const key = buildKey(userId, filename);
  const target = downloadPath(userId, key, outDir);
  const client = new S3Client({ region: REGION });

  let response;
  try {
    response = await client.send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
  } catch (err) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404) {
      console.log("File not found: %s", key);
      return;
    }
    throw err;
  }
  if (response.ContentLength > MAX_DOWNLOAD_BYTES) {
    throw new Error(`File is larger than the ${MAX_DOWNLOAD_BYTES} byte download limit`);
  }

  const chunks = [];
  let total = 0;
  for await (const chunk of response.Body) {
    total += chunk.length;
    if (total > MAX_DOWNLOAD_BYTES) {
      throw new Error(`File is larger than the ${MAX_DOWNLOAD_BYTES} byte download limit`);
    }
    chunks.push(chunk);
  }

  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  fs.writeFileSync(target, Buffer.concat(chunks), { mode: 0o600 });
  console.log("Downloaded %d bytes to %s", total, target);
}

main().catch((err) => {
  console.error("Error: %s", err.message);
  process.exit(1);
});
