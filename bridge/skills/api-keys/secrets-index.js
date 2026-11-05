#!/usr/bin/env node
/**
 * Per-user index of Secrets Manager API-key names, stored as a small JSON
 * object in the user's own S3 prefix.
 *
 * Why this exists: `list` and the per-user secret limit must know the names of
 * a user's secrets without calling `secretsmanager:ListSecrets`, which has no
 * resource-level permissions (it cannot be scoped to one tenant) and, granted
 * on `*`, lets one user's agent read every other tenant's secret names. The
 * scoped session policy already allows `s3:GetObject/PutObject/DeleteObject`
 * on `arn:aws:s3:::<bucket>/<namespace>/*`, so the index lives there and needs
 * no new permission and no `s3:ListBucket`.
 *
 * Robustness:
 *  - Missing object  -> empty index. A brand-new user, or state from before
 *    this index existed, reads cleanly as empty.
 *  - Corrupt content -> treated as empty; the next write replaces it.
 *  - Concurrent writers -> optimistic concurrency with S3 conditional writes
 *    (If-None-Match for create, If-Match for replace) and a bounded retry, so
 *    two sessions adding different keys both converge.
 *  - Old state (keys created before the index) cannot be enumerated — that is
 *    exactly the capability being removed — but each key is re-indexed the
 *    first time it is touched by name (set/get), and `get`/`retrieve` of a
 *    known name never depended on the index.
 */

const INDEX_VERSION = 1;

/**
 * Index object key under the user's own prefix. The leading-dot path is one
 * the s3-user-files skill refuses to create or address (its sanitize() rejects
 * leading/trailing dots), so a user cannot clobber it through that skill; even
 * if they could, it only holds their own key names.
 */
function indexKey(namespace) {
  return `${namespace}/.openclaw/api-keys-index.json`;
}

function makeClient(region) {
  const { S3Client } = require("@aws-sdk/client-s3");
  return new S3Client({ region: region || process.env.AWS_REGION });
}

async function bodyToString(body) {
  if (!body) return "";
  if (typeof body.transformToString === "function") return body.transformToString("utf-8");
  const chunks = [];
  for await (const chunk of body) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf-8");
}

function dedupeSort(keys) {
  return [...new Set(keys.filter((k) => typeof k === "string" && k))].sort();
}

function isNotFound(err) {
  return err && (err.name === "NoSuchKey" || err.name === "NotFound"
    || err.$metadata?.httpStatusCode === 404);
}

function isPreconditionConflict(err) {
  return err && (err.name === "PreconditionFailed" || err.name === "ConditionalRequestConflict"
    || err.$metadata?.httpStatusCode === 412 || err.$metadata?.httpStatusCode === 409);
}

/**
 * Read the index. Returns { keys: string[], etag: string|null }. A missing or
 * unreadable object reads as an empty index with a null etag.
 */
async function readIndex(client, bucket, namespace) {
  const { GetObjectCommand } = require("@aws-sdk/client-s3");
  let resp;
  try {
    resp = await client.send(new GetObjectCommand({ Bucket: bucket, Key: indexKey(namespace) }));
  } catch (err) {
    if (isNotFound(err)) return { keys: [], etag: null };
    throw err;
  }
  let keys = [];
  try {
    const parsed = JSON.parse(await bodyToString(resp.Body));
    if (parsed && Array.isArray(parsed.keys)) keys = parsed.keys;
  } catch {
    // Corrupt index: treat as empty; the next write replaces it.
  }
  return { keys: dedupeSort(keys), etag: resp.ETag || null };
}

/**
 * Write the index. A null etag creates the object only if it does not exist
 * (If-None-Match); a set etag replaces only that version (If-Match). Returns
 * the new ETag.
 */
async function writeIndex(client, bucket, namespace, keys, etag) {
  const { PutObjectCommand } = require("@aws-sdk/client-s3");
  const input = {
    Bucket: bucket,
    Key: indexKey(namespace),
    Body: JSON.stringify({ version: INDEX_VERSION, keys: dedupeSort(keys) }),
    ContentType: "application/json",
  };
  if (etag) input.IfMatch = etag;
  else input.IfNoneMatch = "*";
  const resp = await client.send(new PutObjectCommand(input));
  return resp?.ETag || null;
}

const MAX_ATTEMPTS = 5;

/**
 * Read-modify-write with bounded retry on a concurrent writer. `mutate(set)`
 * receives a Set of the current names and returns true when it changed it; no
 * write happens when it returns false. Returns the resulting sorted names.
 */
async function updateIndex(client, bucket, namespace, mutate) {
  let lastErr;
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
    const { keys, etag } = await readIndex(client, bucket, namespace);
    const set = new Set(keys);
    if (!mutate(set)) return [...set].sort();
    try {
      await writeIndex(client, bucket, namespace, [...set], etag);
      return [...set].sort();
    } catch (err) {
      lastErr = err;
      if (!isPreconditionConflict(err)) throw err;
      // A concurrent writer won; re-read and re-apply.
    }
  }
  throw lastErr || new Error("api-keys index: concurrent update retries exhausted");
}

async function listKeys(client, bucket, namespace) {
  return (await readIndex(client, bucket, namespace)).keys;
}

function addKey(client, bucket, namespace, name) {
  return updateIndex(client, bucket, namespace, (set) => {
    if (set.has(name)) return false;
    set.add(name);
    return true;
  });
}

function removeKey(client, bucket, namespace, name) {
  // Set.delete returns true only when the name was present, so an absent name
  // is a no-op (no write).
  return updateIndex(client, bucket, namespace, (set) => set.delete(name));
}

module.exports = {
  INDEX_VERSION,
  indexKey,
  makeClient,
  bodyToString,
  readIndex,
  writeIndex,
  updateIndex,
  listKeys,
  addKey,
  removeKey,
  isNotFound,
  isPreconditionConflict,
};
