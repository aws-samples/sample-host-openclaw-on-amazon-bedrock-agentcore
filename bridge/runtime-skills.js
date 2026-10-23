/**
 * Runtime-installed ClawHub skills: manifest + cold-start reinstall.
 *
 * Why this exists: the `clawhub-manage` skill lets a user install ClawHub
 * skills while their microVM is running. Those installs land in /skills, the
 * directory OpenClaw scans (skills.load.extraDirs) and the only place from
 * which a skill can resolve /app/node_modules (Dockerfile symlinks
 * /skills/node_modules -> /app/node_modules). /skills is part of the image
 * layer, not of the per-user state, so it is neither mirrored to the session
 * storage mount nor backed up to S3: every runtime install used to vanish at
 * the next cold start.
 *
 * Fix: keep a small per-user manifest of runtime installs (slug + pinned
 * version + sha256 of the installed files) INSIDE the state that already survives a cold start
 * (~/.openclaw/runtime-skills.json, mirrored to /mnt/workspace/.openclaw by
 * state-storage.js and backed up to S3 by workspace-sync.js) and reinstall
 * from it in the background once the gateway is ready.
 *
 * Shared by the skill scripts (install/uninstall/list, which write the
 * manifest) and agentcore-contract.js (which reinstalls from it). No npm
 * dependencies, so it can be copied next to the skill scripts in the image.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { execFile } = require("child_process");

/** Where ClawHub installs skills and where OpenClaw looks for them. */
const SKILLS_DIR = "/skills";
/** Manifest file name inside the OpenClaw state dir. */
const MANIFEST_NAME = "runtime-skills.json";
/** Session-storage mirror of the state dir (see state-storage.js). */
const DEFAULT_MOUNT_STATE_DIR = "/mnt/workspace/.openclaw";
const MANIFEST_VERSION = 1;

/** Per-skill `clawhub install` budget during background reinstall. */
const REINSTALL_TIMEOUT_MS = 90_000;
/**
 * Total time the cold-start reinstall may spend before it stops starting new
 * `clawhub install` runs. Activation waits for the reinstall, so without a
 * bound a long manifest or an unreachable registry would hold the user's
 * session for (entries x 90 s). Skills left over are reported as deferred.
 */
const REINSTALL_TOTAL_BUDGET_MS = 180_000;
/** Bounds for hashing an installed skill tree (see computeSkillIntegrity). */
const INTEGRITY_MAX_ENTRIES = 5_000;
const INTEGRITY_MAX_BYTES = 64 * 1024 * 1024;

// ClawHub slugs: lowercase letters, digits and hyphens, must start with a
// letter (clawhub's own sanitizeSlug lowercases and strips anything else).
// Owner-qualified specs (@owner/slug) are deliberately NOT accepted: clawhub
// installs those under /skills/@owner/<slug>, which OpenClaw does not scan.
const SLUG_RE = /^[a-z][a-z0-9-]{0,63}$/;
// Semver as ClawHub publishes it. Git-backed installs report a commit hash
// instead, which `--version` cannot re-resolve; those are stored unpinned.
const VERSION_RE = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
// sha256 of the installed skill tree, recorded at install time.
const INTEGRITY_RE = /^sha256:[0-9a-f]{64}$/;
// clawhub's own install metadata inside <skillsDir>/<slug> (installedAt
// timestamp etc.); differs between two installs of the same files.
const INTEGRITY_EXCLUDE = new Set([".clawhub", ".clawdhub"]);

/** Validate a ClawHub skill name; returns the lowercased slug or throws. */
function validateSkillName(name) {
  if (!name || typeof name !== "string") {
    throw new Error("Skill name is required.");
  }
  const slug = name.toLowerCase();
  if (!SLUG_RE.test(slug)) {
    throw new Error(
      `Invalid skill name: "${name}" — must start with a letter and contain only letters, numbers, and hyphens (max 64 chars).`,
    );
  }
  return slug;
}

function isValidSlug(slug) {
  return typeof slug === "string" && SLUG_RE.test(slug);
}

function isValidVersion(version) {
  return typeof version === "string" && version.length <= 64 && VERSION_RE.test(version);
}

function isValidIntegrity(value) {
  return typeof value === "string" && INTEGRITY_RE.test(value);
}

/**
 * `sha256:<hex>` over the installed tree `<skillsDir>/<slug>`: every entry's
 * relative POSIX path in sorted order, a type tag, and the file bytes or
 * symlink target. The top-level .clawhub/.clawdhub metadata dirs are left out
 * so two installs of the same artifact hash the same. Returns null when the
 * skill is missing, unreadable, or larger than the hashing bounds.
 */
function computeSkillIntegrity(slug, skillsDir = SKILLS_DIR) {
  if (!isValidSlug(slug)) return null;
  const root = path.join(skillsDir, slug);
  const hash = crypto.createHash("sha256");
  let entries = 0;
  let bytes = 0;
  const walk = (rel) => {
    const names = fs.readdirSync(path.join(root, rel)).sort();
    for (const name of names) {
      if (!rel && INTEGRITY_EXCLUDE.has(name)) continue;
      const childRel = rel ? `${rel}/${name}` : name;
      const abs = path.join(root, childRel);
      const st = fs.lstatSync(abs);
      if (++entries > INTEGRITY_MAX_ENTRIES) throw new Error("too many files");
      if (st.isDirectory()) {
        hash.update(`D\0${childRel}\0`);
        walk(childRel);
      } else if (st.isSymbolicLink()) {
        hash.update(`L\0${childRel}\0${fs.readlinkSync(abs)}\0`);
      } else if (st.isFile()) {
        bytes += st.size;
        if (bytes > INTEGRITY_MAX_BYTES) throw new Error("too large");
        const content = fs.readFileSync(abs);
        hash.update(`F\0${childRel}\0${content.length}\0`);
        hash.update(content);
      } else {
        hash.update(`X\0${childRel}\0`);
      }
    }
  };
  try {
    if (!fs.lstatSync(root).isDirectory()) return null;
    walk("");
  } catch {
    return null;
  }
  return `sha256:${hash.digest("hex")}`;
}

/** The OpenClaw state dir as the gateway (and its exec children) see it. */
function resolveStateDir(env = process.env) {
  const explicit = (env.OPENCLAW_STATE_DIR || "").trim();
  if (explicit) return explicit;
  return path.join(env.HOME || "/root", ".openclaw");
}

function manifestPath(stateDir = resolveStateDir()) {
  return path.join(stateDir, MANIFEST_NAME);
}

function emptyManifest() {
  return { version: MANIFEST_VERSION, skills: {} };
}

/**
 * Read the manifest. Never throws: a missing file is an empty manifest, a
 * corrupt one is reported via `corrupt: true` and treated as empty, and
 * entries that fail slug/version/integrity validation are dropped (counted in
 * `invalid`) so nothing unvalidated ever reaches the CLI.
 *
 * @returns {{ version: number, skills: Record<string, {version: string|null, integrity?: string, installedAt?: string}>, corrupt: boolean, invalid: number }}
 */
function readManifest(file = manifestPath(), { log = console } = {}) {
  const result = { ...emptyManifest(), corrupt: false, invalid: 0 };
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (err) {
    if (err.code !== "ENOENT") {
      log.warn(`[runtime-skills] Cannot read manifest ${file}: ${err.message}`);
      result.corrupt = true;
    }
    return result;
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    log.warn(`[runtime-skills] Manifest ${file} is not valid JSON (${err.message}) — treating as empty`);
    result.corrupt = true;
    return result;
  }
  if (!parsed || typeof parsed !== "object" || !parsed.skills || typeof parsed.skills !== "object" || Array.isArray(parsed.skills)) {
    log.warn(`[runtime-skills] Manifest ${file} has an unexpected shape — treating as empty`);
    result.corrupt = true;
    return result;
  }
  for (const [slug, entry] of Object.entries(parsed.skills)) {
    if (!isValidSlug(slug)) {
      result.invalid++;
      log.warn(`[runtime-skills] Dropping manifest entry with invalid slug: ${JSON.stringify(slug)}`);
      continue;
    }
    const version = entry && typeof entry === "object" ? entry.version : undefined;
    if (version !== null && version !== undefined && !isValidVersion(version)) {
      result.invalid++;
      log.warn(`[runtime-skills] Dropping manifest entry ${slug}: invalid version ${JSON.stringify(version)}`);
      continue;
    }
    const integrity = entry && typeof entry === "object" ? entry.integrity : undefined;
    if (integrity !== null && integrity !== undefined && !isValidIntegrity(integrity)) {
      result.invalid++;
      log.warn(`[runtime-skills] Dropping manifest entry ${slug}: invalid integrity value`);
      continue;
    }
    result.skills[slug] = {
      version: isValidVersion(version) ? version : null,
      ...(isValidIntegrity(integrity) ? { integrity } : {}),
      ...(entry && typeof entry.installedAt === "string" ? { installedAt: entry.installedAt } : {}),
    };
  }
  return result;
}

/** Atomically write the manifest (tmp file + rename in the same directory). */
function writeManifest(file, manifest) {
  const out = { version: MANIFEST_VERSION, skills: {} };
  for (const slug of Object.keys(manifest.skills || {}).sort()) {
    const entry = manifest.skills[slug];
    if (!isValidSlug(slug)) continue;
    out.skills[slug] = {
      version: isValidVersion(entry?.version) ? entry.version : null,
      ...(isValidIntegrity(entry?.integrity) ? { integrity: entry.integrity } : {}),
      ...(entry?.installedAt ? { installedAt: entry.installedAt } : {}),
    };
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  // `.tmp-<digits>` is what state-storage.js / workspace-sync.js treat as a
  // transient file, so a mirror that runs mid-write never copies it.
  const tmp = `${file}.tmp-${Date.now()}${process.pid}`;
  fs.writeFileSync(tmp, `${JSON.stringify(out, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, file);
  return out;
}

/**
 * Copy the manifest straight onto the session-storage mirror so it survives
 * even if the microVM dies before the next periodic state mirror. Best effort:
 * no mount, no copy. Returns true when copied.
 */
function mirrorManifest(file, mountStateDir = DEFAULT_MOUNT_STATE_DIR, { log = console } = {}) {
  try {
    if (!fs.existsSync(mountStateDir) || !fs.statSync(mountStateDir).isDirectory()) return false;
    if (!fs.existsSync(file)) return false;
    const dest = path.join(mountStateDir, path.basename(file));
    const tmp = `${dest}.tmp-${Date.now()}${process.pid}`;
    fs.copyFileSync(file, tmp);
    fs.renameSync(tmp, dest);
    return true;
  } catch (err) {
    log.warn(`[runtime-skills] Could not mirror manifest to ${mountStateDir}: ${err.message}`);
    return false;
  }
}

/**
 * Record a runtime install. `version` may be null (unpinned: latest on
 * reinstall). `opts.integrity` is the computeSkillIntegrity() digest of what
 * was just installed; the cold-start reinstall refuses files that do not
 * match it. Without one the entry is reinstalled unverified, as before.
 */
function recordInstall(file, slug, version, opts = {}) {
  const validSlug = validateSkillName(slug);
  const manifest = readManifest(file, opts);
  manifest.skills[validSlug] = {
    version: isValidVersion(version) ? version : null,
    ...(isValidIntegrity(opts.integrity) ? { integrity: opts.integrity } : {}),
    installedAt: new Date().toISOString(),
  };
  return writeManifest(file, manifest);
}

/** Forget a runtime install. Returns true if the slug was in the manifest. */
function recordUninstall(file, slug, opts = {}) {
  const validSlug = validateSkillName(slug);
  const manifest = readManifest(file, opts);
  const had = Object.prototype.hasOwnProperty.call(manifest.skills, validSlug);
  if (had) {
    delete manifest.skills[validSlug];
    writeManifest(file, manifest);
  }
  return had;
}

/**
 * Call `onChange(file)` shortly after the manifest is (re)written. The S3
 * backup of the state dir runs every 30 minutes and StopRuntimeSession does
 * not give the container a chance to flush, so an install/uninstall made
 * less than 30 minutes before a cold start would otherwise be lost (verified
 * on staging: uninstall → StopRuntimeSession → the stale S3 manifest
 * reinstalled the skill). The caller uses this to back up just the manifest
 * right away.
 *
 * Watches the manifest's directory (the file is replaced by rename, so a
 * watcher on the file itself would go stale) and ignores our own
 * `.tmp-*` files. Debounced. Returns `{ close() }`; `null` when fs.watch is
 * unavailable (logged, non-fatal).
 */
function watchManifest(file, onChange, { debounceMs = 1500, log = console } = {}) {
  const dir = path.dirname(file);
  const name = path.basename(file);
  let timer = null;
  let watcher;
  try {
    fs.mkdirSync(dir, { recursive: true });
    watcher = fs.watch(dir, { persistent: false }, (_event, changed) => {
      if (changed && changed.toString() !== name) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        try {
          Promise.resolve(onChange(file)).catch((err) =>
            log.warn(`[runtime-skills] Manifest change handler failed: ${err.message}`),
          );
        } catch (err) {
          log.warn(`[runtime-skills] Manifest change handler failed: ${err.message}`);
        }
      }, debounceMs);
    });
    watcher.on("error", (err) => log.warn(`[runtime-skills] Manifest watcher error: ${err.message}`));
  } catch (err) {
    log.warn(`[runtime-skills] Cannot watch manifest (${err.message}) — changes are backed up only by the periodic save`);
    return null;
  }
  return {
    close() {
      if (timer) clearTimeout(timer);
      timer = null;
      watcher.close();
    },
  };
}

/** True when `<skillsDir>/<slug>/SKILL.md` exists. */
function isSkillPresent(slug, skillsDir = SKILLS_DIR) {
  if (!isValidSlug(slug)) return false;
  return fs.existsSync(path.join(skillsDir, slug, "SKILL.md"));
}

/**
 * Version clawhub recorded for an installed skill
 * (`<skillsDir>/<slug>/.clawhub/origin.json` -> installedVersion), or null
 * when unreadable or not a semver string (git-backed installs store a commit).
 */
function readInstalledVersion(slug, skillsDir = SKILLS_DIR) {
  if (!isValidSlug(slug)) return null;
  for (const dot of [".clawhub", ".clawdhub"]) {
    try {
      const origin = JSON.parse(fs.readFileSync(path.join(skillsDir, slug, dot, "origin.json"), "utf8"));
      const v = origin && origin.installedVersion;
      return isValidVersion(v) ? v : null;
    } catch {
      // try next / fall through
    }
  }
  return null;
}

/**
 * argv for `clawhub install`. No `--force`: clawhub uses that flag both to
 * overwrite an existing folder AND to bypass its "flagged for security
 * review" check in non-interactive mode. Without it a suspicious skill fails
 * with "Use --force to install suspicious skills in non-interactive mode",
 * which install.js turns into a clear refusal. Malware-flagged skills are
 * always refused by clawhub. --workdir/--dir pin the install location so it
 * does not depend on the caller's cwd (clawhub otherwise installs under
 * `<cwd>/skills`).
 */
function installArgs(slug, version = null, skillsDir = SKILLS_DIR) {
  const validSlug = validateSkillName(slug);
  if (version !== null && version !== undefined && !isValidVersion(version)) {
    throw new Error(`Invalid skill version: ${JSON.stringify(version)}`);
  }
  return [
    "install",
    validSlug,
    ...(version ? ["--version", version] : []),
    "--no-input",
    "--workdir",
    skillsDir,
    "--dir",
    skillsDir,
  ];
}

/** argv for `clawhub uninstall` (clawhub needs --yes when prompts are off). */
function uninstallArgs(slug, skillsDir = SKILLS_DIR) {
  const validSlug = validateSkillName(slug);
  return ["uninstall", validSlug, "--yes", "--no-input", "--workdir", skillsDir, "--dir", skillsDir];
}

/** True when clawhub's stderr/stdout says the skill is flagged for review. */
function isSecurityRefusal(text) {
  return /flagged for ClawHub security review|install suspicious skills|flagged as malware|flagged as malicious/i.test(
    String(text || ""),
  );
}

/**
 * Minimal environment for the clawhub child: PATH/HOME/locale plus clawhub's
 * own CLAWHUB_ / CLAWDHUB_ settings. No AWS credential variables — clawhub
 * only talks to the ClawHub registry.
 */
function clawhubEnv(base = process.env) {
  const env = {};
  for (const key of ["PATH", "HOME", "LANG", "LC_ALL", "NODE_PATH", "TMPDIR"]) {
    if (base[key]) env[key] = base[key];
  }
  for (const [key, value] of Object.entries(base)) {
    if (/^(CLAWHUB|CLAWDHUB)_/.test(key) && !/WORKDIR$/.test(key)) env[key] = value;
  }
  if (!env.HOME) env.HOME = os.homedir();
  return env;
}

/** Default runner: `clawhub <args>` via execFile (no shell). */
function runClawhub(args, { timeoutMs = REINSTALL_TIMEOUT_MS, env = clawhubEnv() } = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      "clawhub",
      args,
      { encoding: "utf8", timeout: timeoutMs, env, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) {
          err.stdout = stdout;
          err.stderr = stderr;
          reject(err);
        } else {
          resolve({ stdout, stderr });
        }
      },
    );
  });
}

/**
 * Reinstall every skill in the manifest that is not already present in
 * `skillsDir`. Sequential (one ClawHub download at a time), never throws,
 * never touches the manifest: a failed reinstall stays recorded so the next
 * cold start tries again. Returns a summary suitable for logging.
 *
 * The outcome is informational: activation continues whatever it is, so an
 * entry that can never be restored (unpublished slug, registry outage,
 * mismatching files) costs that one skill, not the user's session.
 *
 * - An entry with a recorded `integrity` is re-hashed after the install; on a
 *   mismatch the installed directory is removed before OpenClaw can load it
 *   and the entry counts as failed. Entries recorded before integrity existed
 *   are reinstalled unverified (counted in `unverified`).
 * - No new install starts once `totalBudgetMs` has elapsed; the rest are
 *   counted in `deferred` and retried at the next cold start.
 *
 * @param {object} opts
 * @param {string} [opts.manifestFile]
 * @param {string} [opts.skillsDir]
 * @param {(args: string[]) => Promise<{stdout: string, stderr: string}>} [opts.run]  injected for tests
 * @param {object} [opts.log]
 * @param {number} [opts.totalBudgetMs]
 * @param {() => number} [opts.now]  injected for tests
 * @returns {Promise<{ total: number, reinstalled: number, present: number, failed: number, deferred: number, unverified: number, corrupt: boolean, invalid: number, results: Array<{slug: string, version: string|null, status: string, error?: string}> }>}
 */
async function reinstallFromManifest({
  manifestFile = manifestPath(),
  skillsDir = SKILLS_DIR,
  run = runClawhub,
  log = console,
  totalBudgetMs = REINSTALL_TOTAL_BUDGET_MS,
  now = Date.now,
} = {}) {
  const summary = {
    total: 0, reinstalled: 0, present: 0, failed: 0, deferred: 0, unverified: 0,
    corrupt: false, invalid: 0, results: [],
  };
  let manifest;
  try {
    manifest = readManifest(manifestFile, { log });
  } catch (err) {
    log.warn(`[runtime-skills] Manifest read failed: ${err.message}`);
    return { ...summary, corrupt: true };
  }
  summary.corrupt = manifest.corrupt;
  summary.invalid = manifest.invalid;
  const entries = Object.entries(manifest.skills);
  summary.total = entries.length;
  if (entries.length === 0) return summary;

  const deadline = now() + totalBudgetMs;
  for (const [slug, entry] of entries) {
    const version = entry.version || null;
    const label = `${slug}${version ? `@${version}` : ""}`;
    if (isSkillPresent(slug, skillsDir)) {
      summary.present++;
      summary.results.push({ slug, version, status: "present" });
      continue;
    }
    if (now() >= deadline) {
      summary.deferred++;
      summary.results.push({ slug, version, status: "deferred" });
      log.warn(`[runtime-skills] Reinstall budget of ${totalBudgetMs}ms used up; deferring ${label} to the next session`);
      continue;
    }
    let args;
    try {
      args = installArgs(slug, version, skillsDir);
    } catch (err) {
      summary.failed++;
      summary.results.push({ slug, version, status: "failed", error: err.message });
      log.warn(`[runtime-skills] Skipping ${slug}: ${err.message}`);
      continue;
    }
    const started = now();
    try {
      await run(args);
      if (!isSkillPresent(slug, skillsDir)) {
        throw new Error(`clawhub exited 0 but ${path.join(skillsDir, slug, "SKILL.md")} is missing`);
      }
      if (entry.integrity) {
        const actual = computeSkillIntegrity(slug, skillsDir);
        if (actual !== entry.integrity) {
          let removed = "removed";
          try {
            fs.rmSync(path.join(skillsDir, slug), { recursive: true, force: true });
          } catch (rmErr) {
            removed = `could NOT be removed (${rmErr.message})`;
          }
          throw new Error(
            `installed files do not match the checksum recorded at install time (expected ${entry.integrity}, got ${actual || "unreadable"}); ${removed}. Install the skill again to accept the current files.`,
          );
        }
      } else {
        summary.unverified++;
      }
      summary.reinstalled++;
      summary.results.push({ slug, version, status: "reinstalled", verified: !!entry.integrity });
      log.log(`[runtime-skills] Reinstalled ${label}${entry.integrity ? " (checksum verified)" : " (no recorded checksum)"} in ${now() - started}ms`);
    } catch (err) {
      const detail = String(err.stderr || err.stdout || err.message || err).trim().split("\n").slice(-3).join(" | ");
      summary.failed++;
      summary.results.push({ slug, version, status: "failed", error: detail });
      log.warn(`[runtime-skills] Reinstall of ${label} failed after ${now() - started}ms: ${detail}`);
    }
  }
  return summary;
}

module.exports = {
  SKILLS_DIR,
  MANIFEST_NAME,
  DEFAULT_MOUNT_STATE_DIR,
  REINSTALL_TIMEOUT_MS,
  REINSTALL_TOTAL_BUDGET_MS,
  validateSkillName,
  isValidSlug,
  isValidVersion,
  isValidIntegrity,
  computeSkillIntegrity,
  resolveStateDir,
  manifestPath,
  readManifest,
  writeManifest,
  mirrorManifest,
  recordInstall,
  recordUninstall,
  watchManifest,
  isSkillPresent,
  readInstalledVersion,
  installArgs,
  uninstallArgs,
  isSecurityRefusal,
  clawhubEnv,
  runClawhub,
  reinstallFromManifest,
};
