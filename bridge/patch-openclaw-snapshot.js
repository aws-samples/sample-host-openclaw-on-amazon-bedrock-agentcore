/**
 * Apply the snapshot patches declared in patches/runtime-v2.json to OpenClaw.
 *
 * The base and preinit prerequisites must match their pinned hashes first.
 * Targets must be regular package files without symlink redirection, and each
 * replacement anchor and resulting hash is checked before any file is written.
 * Already-patched hashes are accepted so the same delta can be reapplied.
 */
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const manifest = require("./patches/runtime-v2.json");
const sha256 = source => createHash("sha256").update(source).digest("hex");

function patchSource(file, source) {
  const pin = manifest.files.find(item => item.file === file);
  if (!pin) throw new Error("Unsupported snapshot patch target");
  const digest = sha256(source);
  if (digest === pin.patchedSha256) return { changed: false, source };
  if (digest !== pin.originalSha256) throw new Error(`Unsupported snapshot source SHA-256: ${file}`);
  let result = source.toString("utf8");
  for (const change of pin.replacements) {
    if (!change.before || result.split(change.before).length !== 2) {
      throw new Error(`Snapshot patch anchor is not unique: ${file}`);
    }
    result = result.replace(change.before, () => change.after);
  }
  const patched = Buffer.from(result, "utf8");
  if (sha256(patched) !== pin.patchedSha256) throw new Error(`Snapshot result SHA-256 mismatch: ${file}`);
  return { changed: true, source: patched };
}

function patchPackage(packageDir) {
  const root = fs.realpathSync(packageDir);
  const read = file => {
    const resolved = path.join(root, file);
    if (!resolved.startsWith(root + path.sep) || fs.realpathSync(resolved) !== resolved ||
        !fs.lstatSync(resolved).isFile()) throw new Error("Snapshot patch target is not a regular package file");
    return fs.readFileSync(resolved);
  };
  const pkg = JSON.parse(read("package.json"));
  if (pkg.name !== "openclaw" || pkg.version !== manifest.version) throw new Error("Unsupported OpenClaw version");
  for (const [file, expected] of Object.entries(manifest.requires)) {
    if (sha256(read(file)) !== expected) throw new Error(`Apply the base and preinit patches first: ${file}`);
  }
  const results = manifest.files.map(pin => ({ ...pin, ...patchSource(pin.file, read(pin.file)) }));
  // Validate the entire delta before changing any vendor file.
  for (const result of results) {
    if (result.changed) fs.writeFileSync(path.join(root, result.file), result.source);
  }
  return {
    version: manifest.version,
    changed: results.some(result => result.changed),
    files: results.map(({ file, changed, patchedSha256 }) => ({ file, changed, sha256: patchedSha256 })),
  };
}

if (require.main === module) {
  try {
    if (process.argv.length !== 3) throw new Error("Usage: node patch-openclaw-snapshot.js <openclaw-package-dir>");
    const result = patchPackage(process.argv[2]);
    console.log(`[patch-openclaw-snapshot] OpenClaw ${result.version}: snapshot patches ${result.changed ? "applied" : "already applied"}.`);
  } catch (error) {
    console.error(`[patch-openclaw-snapshot] ${error.message}`);
    process.exitCode = 1;
  }
}
module.exports = { OPENCLAW_VERSION: manifest.version, PATCHES: manifest.files, sha256, patchSource, patchPackage };
