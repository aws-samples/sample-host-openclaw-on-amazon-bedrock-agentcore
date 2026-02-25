#!/usr/bin/env node
/**
 * write_user_file — Write content to a user's S3-namespaced file.
 * Usage: node write.js <user_id> <filename> <content...>
 */
const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");
const {
  BUCKET,
  REGION,
  buildKey,
  validateUserId,
  validateBucket,
} = require("./common");

/**
 * Read all data from stdin as a string.
 */
function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(chunks.join("")));
    process.stdin.on("error", reject);
  });
}

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];
  // Read content from argv or stdin (--stdin flag). Stdin avoids OS ARG_MAX limits.
  const argContent = process.argv.slice(4).join(" ");
  const content = argContent === "--stdin" ? await readStdin() : argContent;

  validateUserId(userId);
  validateBucket();

  if (!filename) {
    console.error("Error: filename argument is required.");
    process.exit(1);
  }
  if (!content) {
    console.error("Error: content argument is required.");
    process.exit(1);
  }

  const MAX_CONTENT_BYTES = 1 * 1024 * 1024; // 1 MB
  if (Buffer.byteLength(content, "utf-8") > MAX_CONTENT_BYTES) {
    console.error("Error: content exceeds maximum allowed size (1 MB).");
    process.exit(1);
  }

  const key = buildKey(userId, filename);
  const client = new S3Client({ region: REGION });

  await client.send(
    new PutObjectCommand({
      Bucket: BUCKET,
      Key: key,
      Body: content,
      ContentType: "text/plain; charset=utf-8",
    }),
  );

  console.log(`File written: ${key} (${content.length} bytes)`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
