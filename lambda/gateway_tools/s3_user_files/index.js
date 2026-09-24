/**
 * Gateway Lambda MCP target: user-files (list_files / read_file / write_file / delete_file).
 *
 * Same object layout as the exec skill bridge/skills/s3-user-files
 * (`<namespace>/<sanitized filename>` in the openclaw-user-files bucket), so a
 * file written through either surface is visible through the other.
 *
 * The namespace comes ONLY from the verified caller token (lib/identity.js).
 * There is no `user_id` argument: the model cannot pick a namespace.
 *
 * Runtime: nodejs22.x, which bundles AWS SDK for JavaScript v3.
 */
"use strict";

const { createVerifier, resolveCaller } = require("../lib/identity");
const { toolNameFromContext, sanitizeFilename, withErrorEnvelope } = require("../lib/mcp");

const MAX_CONTENT_BYTES = 1 * 1024 * 1024;

/**
 * `deps` lets tests inject a fake S3 client and verifier.
 *   deps.s3.send(command) -> Promise
 *   deps.commands = { ListObjectsV2Command, GetObjectCommand, PutObjectCommand, DeleteObjectCommand }
 */
function createHandler(deps = {}) {
  const bucket = deps.bucket || process.env.S3_USER_FILES_BUCKET;
  if (!bucket) throw new Error("S3_USER_FILES_BUCKET must be set");
  const verifier = deps.verifier || createVerifier();

  let s3 = deps.s3;
  let commands = deps.commands;
  function client() {
    if (!s3) {
      const sdk = require("@aws-sdk/client-s3");
      s3 = new sdk.S3Client({});
      commands = sdk;
    }
    return { s3, commands };
  }

  async function bodyToString(body) {
    if (!body) return "";
    if (typeof body === "string") return body;
    if (typeof body.transformToString === "function") return body.transformToString("utf-8");
    const chunks = [];
    for await (const chunk of body) chunks.push(chunk);
    return Buffer.concat(chunks).toString("utf-8");
  }

  const tools = {
    async list_files(ns) {
      const { s3, commands } = client();
      const prefix = `${ns}/`;
      const resp = await s3.send(
        new commands.ListObjectsV2Command({ Bucket: bucket, Prefix: prefix, MaxKeys: 1000 }),
      );
      const files = (resp.Contents || [])
        .filter((o) => o.Key !== prefix)
        .map((o) => ({
          name: o.Key.slice(prefix.length),
          size: o.Size,
          modified: o.LastModified ? new Date(o.LastModified).toISOString() : null,
        }));
      return { files, truncated: Boolean(resp.IsTruncated) };
    },

    async read_file(ns, args) {
      const { s3, commands } = client();
      const key = `${ns}/${sanitizeFilename(args.filename)}`;
      try {
        const resp = await s3.send(new commands.GetObjectCommand({ Bucket: bucket, Key: key }));
        return { filename: key.slice(ns.length + 1), content: await bodyToString(resp.Body) };
      } catch (err) {
        if (err && (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404)) {
          return { error: "not_found", message: `No file named ${args.filename}` };
        }
        throw err;
      }
    },

    async write_file(ns, args) {
      const { s3, commands } = client();
      if (typeof args.content !== "string" || !args.content) {
        throw new Error("content is required");
      }
      const bytes = Buffer.byteLength(args.content, "utf-8");
      if (bytes > MAX_CONTENT_BYTES) throw new Error("content exceeds maximum allowed size (1 MiB)");
      const key = `${ns}/${sanitizeFilename(args.filename)}`;
      await s3.send(
        new commands.PutObjectCommand({
          Bucket: bucket,
          Key: key,
          Body: args.content,
          ContentType: "text/plain; charset=utf-8",
        }),
      );
      return { written: key.slice(ns.length + 1), bytes };
    },

    async delete_file(ns, args) {
      const { s3, commands } = client();
      const key = `${ns}/${sanitizeFilename(args.filename)}`;
      await s3.send(new commands.DeleteObjectCommand({ Bucket: bucket, Key: key }));
      return { deleted: key.slice(ns.length + 1) };
    },
  };

  return withErrorEnvelope(async (event, context) => {
    const { identity, args } = await resolveCaller(event, verifier);
    const tool = toolNameFromContext(context);
    const fn = tools[tool];
    if (!fn) return { error: "unknown_tool", message: `Unknown tool "${tool}"` };
    return fn(identity.namespace, args);
  });
}

let _handler = null;
async function handler(event, context) {
  if (!_handler) _handler = createHandler();
  return _handler(event, context);
}

module.exports = { handler, createHandler, MAX_CONTENT_BYTES };
