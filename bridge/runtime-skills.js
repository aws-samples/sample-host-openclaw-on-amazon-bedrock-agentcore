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
 * version + sha256 digest of the installed files) INSIDE the state that
 * already survives a cold start (~/.openclaw/runtime-skills.json, mirrored
 * to /mnt/workspace/.openclaw by state-storage.js and backed up to S3 by
 * workspace-sync.js) and reinstall from it in the background once the
 * gateway is ready.
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

/**
 * Consecutive-failure threshold after which a manifest entry is quarantined
 * (marked `disabled: true` and skipped on future cold starts). Each entry
 * tracks its own failure streak in the manifest (`failures`), reset to 0 on a
 * successful reinstall. This bounds the self-sustaining lockout a persistently
 * unresolvable slug (upstream unpublish, registry outage, or a prompt-injected
 * bad entry) would otherwise cause: once quarantined the slug stops counting
 * toward activation health and the user can re-enable it with a fresh
 * `clawhub install` through clawhub-manage.
 */
const MAX_CONSECUTIVE_FAILURES = 3;

/**
 * Why an entry was disabled. `failures`: MAX_CONSECUTIVE_FAILURES reinstall
 * failures in a row. `integrity`: the reinstalled files did not match the
 * digest recorded at install time (retrying would fetch the same files, so
 * the entry is disabled at once).
 */
const DISABLED_REASONS = new Set(["failures", "integrity"]);

// ClawHub slugs: lowercase letters, digits and hyphens, must start with a
// letter (clawhub's own sanitizeSlug lowercases and strips anything else).
// Owner-qualified specs (@owner/slug) are deliberately NOT accepted: clawhub
// installs those under /skills/@owner/<slug>, which OpenClaw does not scan.
const SLUG_RE = /^[a-z][a-z0-9-]{0,63}$/;
// Semver as ClawHub publishes it. Git-backed installs report a commit hash
// instead, which `--version` cannot re-resolve; those are stored unpinned.
const VERSION_RE = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;

/**
 * Operator-controlled allowlist of ClawHub slugs that may be (re)installed
 * at runtime. Baked into the container by the deployment operator via the
 * `RUNTIME_SKILLS_ALLOWLIST` env var (comma- or whitespace-separated) and
 * therefore NOT writable by the agent context: the agent runs as root in
 * the microVM but it cannot rewrite its own process environment to add
 * entries that the contract process will honour at cold start, and the
 * value does not live in `.openclaw/` so it is not persisted/restored from
 * S3 either. When the variable is unset or empty, no allowlist is
 * enforced (back-compat: the pre-existing SLUG_RE / no-`--force` /
 * ClawHub security-review guards still apply). When it is set, any slug
 * missing from the list is refused at both install time (install.js) and
 * cold-start reinstall time (reinstallFromManifest), so a manifest entry
 * poisoned by a prompt-injected turn cannot bring in a slug the operator
 * never approved.
 */
const ALLOWLIST_ENV = "RUNTIME_SKILLS_ALLOWLIST";

/**
 * Parse the operator allowlist from an env map. Returns `null` when the
 * variable is unset or empty (meaning "no allowlist, back-compat"), and a
 * Set<string> of validated lowercase slugs otherwise. Invalid tokens are
 * dropped with a warning so a typo in the operator config fails loudly
 * rather than silently permitting everything.
 */
function parseAllowlist(env = process.env, { log = console } = {}) {
  const raw = env[ALLOWLIST_ENV];
  if (raw === undefined || raw === null) return null;
  const trimmed = String(raw).trim();
  if (!trimmed) return null;
  const tokens = trimmed.split(/[\s,]+/).filter(Boolean);
  const result = new Set();
  for (const token of tokens) {
    const slug = token.toLowerCase();
    if (!SLUG_RE.test(slug)) {
      log.warn(`[runtime-skills] Ignoring invalid entry in ${ALLOWLIST_ENV}: ${JSON.stringify(token)}`);
      continue;
    }
    result.add(slug);
  }
  return result;
}

/**
 * True when the slug is permitted by the operator-controlled allowlist.
 * Returns true unconditionally when no allowlist is configured.
 */
function isAllowed(slug, allowlist) {
  if (!allowlist) return true;
  return typeof slug === "string" && allowlist.has(slug.toLowerCase());
}

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

// Integrity digest recorded per manifest entry: sha256 of the installed skill
// tree (see computeSkillIntegrity). Validated on read/write.
const INTEGRITY_RE = /^sha256:[0-9a-f]{64}$/;

function isValidIntegrity(value) {
  return typeof value === "string" && INTEGRITY_RE.test(value);
}

// Top-level directories inside <skillsDir>/<slug> left out of the digest:
// clawhub writes install metadata there (install timestamp, registry cache)
// that differs between two installs of the same version.
const INTEGRITY_EXCLUDE_DIRS = new Set([".clawhub", ".clawdhub"]);

/**
 * Digests of the skill tree at `<skillsDir>/<slug>`, each as `"sha256:<hex>"`,
 * or null when the directory is missing, unreadable, or larger than the
 * hashing bounds. Both cover every entry's relative POSIX path in sorted
 * order, a type tag, and the file bytes or symlink target (links are not
 * followed); the top-level .clawhub/.clawdhub metadata dirs are left out so
 * two installs of the same artifact hash the same.
 *
 * `current` is the format recorded by this version. `legacy` is the format
 * earlier fork builds recorded (an extra separator after each file's bytes
 * and the mode of special files); it is only used to verify entries those
 * builds wrote, which are then re-recorded in the current format.
 */
function computeSkillDigests(slug, skillsDir = SKILLS_DIR) {
  if (!isValidSlug(slug)) return null;
  const root = path.join(skillsDir, slug);
  const current = crypto.createHash("sha256");
  const legacy = crypto.createHash("sha256");
  const both = (s) => { current.update(s); legacy.update(s); };
  let entries = 0;
  let bytes = 0;
  const walk = (rel) => {
    const names = fs.readdirSync(path.join(root, rel)).sort();
    for (const name of names) {
      if (!rel && INTEGRITY_EXCLUDE_DIRS.has(name)) continue;
      const childRel = rel ? `${rel}/${name}` : name;
      const abs = path.join(root, childRel);
      const st = fs.lstatSync(abs);
      if (++entries > INTEGRITY_MAX_ENTRIES) throw new Error("too many files");
      if (st.isDirectory()) {
        both(`D\0${childRel}\0`);
        walk(childRel);
      } else if (st.isSymbolicLink()) {
        both(`L\0${childRel}\0${fs.readlinkSync(abs)}\0`);
      } else if (st.isFile()) {
        bytes += st.size;
        if (bytes > INTEGRITY_MAX_BYTES) throw new Error("too large");
        const content = fs.readFileSync(abs);
        both(`F\0${childRel}\0${content.length}\0`);
        both(content);
        legacy.update("\0");
      } else {
        current.update(`X\0${childRel}\0`);
        legacy.update(`X\0${childRel}\0${st.mode}\0`);
      }
    }
  };
  try {
    if (!fs.lstatSync(root).isDirectory()) return null;
    walk("");
  } catch {
    return null;
  }
  return { current: `sha256:${current.digest("hex")}`, legacy: `sha256:${legacy.digest("hex")}` };
}

/**
 * `sha256:<hex>` over the installed tree `<skillsDir>/<slug>` (see
 * computeSkillDigests), or null when the skill is missing, unreadable, or
 * larger than the hashing bounds.
 */
function computeSkillIntegrity(slug, skillsDir = SKILLS_DIR) {
  const digests = computeSkillDigests(slug, skillsDir);
  return digests ? digests.current : null;
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
 * entries that fail slug/version validation are dropped (counted in
 * `invalid`) so nothing unvalidated ever reaches the CLI.
 *
 * The optional per-entry `failures` and `disabled` fields are preserved so the
 * cold-start reinstall can quarantine an entry that keeps failing (see
 * MAX_CONSECUTIVE_FAILURES / reinstallFromManifest below). The optional
 * provenance fields (`installedBy`, `installedSession`) are informational.
 *
 * `integrity` (sha256 of the installed files) is optional: entries written
 * before it existed have none and are hashed on their next reinstall.
 *
 * @returns {{ version: number, skills: Record<string, {version: string|null, integrity?: string, installedAt?: string, installedBy?: string, installedSession?: string, failures?: number, disabled?: boolean, disabledReason?: string}>, corrupt: boolean, invalid: number }}
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
    const failures = entry && typeof entry === "object" && Number.isInteger(entry.failures) && entry.failures > 0
      ? entry.failures
      : 0;
    const disabled = !!(entry && typeof entry === "object" && entry.disabled === true);
    const normalized = {
      version: isValidVersion(version) ? version : null,
      ...(isValidIntegrity(integrity) ? { integrity } : {}),
      ...(failures > 0 ? { failures } : {}),
      ...(disabled ? { disabled: true } : {}),
    };
    if (disabled && DISABLED_REASONS.has(entry.disabledReason)) normalized.disabledReason = entry.disabledReason;
    if (entry && typeof entry.installedAt === "string") normalized.installedAt = entry.installedAt;
    if (entry && typeof entry.installedBy === "string" && entry.installedBy.length <= 256) {
      normalized.installedBy = entry.installedBy;
    }
    if (entry && typeof entry.installedSession === "string" && entry.installedSession.length <= 256) {
      normalized.installedSession = entry.installedSession;
    }
    result.skills[slug] = normalized;
  }
  return result;
}

/** Atomically write the manifest (tmp file + rename in the same directory). */
function writeManifest(file, manifest) {
  const out = { version: MANIFEST_VERSION, skills: {} };
  for (const slug of Object.keys(manifest.skills || {}).sort()) {
    const entry = manifest.skills[slug];
    if (!isValidSlug(slug)) continue;
    const failures = Number.isInteger(entry?.failures) && entry.failures > 0 ? entry.failures : 0;
    const persisted = {
      version: isValidVersion(entry?.version) ? entry.version : null,
      ...(isValidIntegrity(entry?.integrity) ? { integrity: entry.integrity } : {}),
      ...(failures > 0 ? { failures } : {}),
      ...(entry?.disabled === true ? { disabled: true } : {}),
    };
    if (entry?.disabled === true && DISABLED_REASONS.has(entry?.disabledReason)) {
      persisted.disabledReason = entry.disabledReason;
    }
    if (entry?.installedAt) persisted.installedAt = entry.installedAt;
    if (typeof entry?.installedBy === "string" && entry.installedBy.length <= 256) {
      persisted.installedBy = entry.installedBy;
    }
    if (typeof entry?.installedSession === "string" && entry.installedSession.length <= 256) {
      persisted.installedSession = entry.installedSession;
    }
    out.skills[slug] = persisted;
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
 * Record a runtime install. `version` may be null when clawhub recorded no
 * semver (e.g. a git-backed install); the reinstall then cannot pin a
 * version, but `opts.integrity` (sha256 of the installed files, see
 * computeSkillIntegrity) still makes it refuse files that differ from the
 * ones installed now. Captures install provenance (`installedAt`, `installedBy`,
 * `installedSession`) so list.js can show who/when/which session added a
 * slug and a reviewer can tell a user-driven install apart from a
 * manifest poisoned by a prompt-injected turn. Provenance is informational
 * only — it is written from the agent context and therefore not an
 * authentication signal — but a missing or inconsistent trail is still
 * a useful red flag.
 */
function recordInstall(file, slug, version, opts = {}) {
  const validSlug = validateSkillName(slug);
  const manifest = readManifest(file, opts);
  const env = opts.env || process.env;
  const entry = {
    version: isValidVersion(version) ? version : null,
    ...(isValidIntegrity(opts.integrity) ? { integrity: opts.integrity } : {}),
    installedAt: new Date().toISOString(),
    // A fresh install clears any previous quarantine / failure streak so the
    // cold-start reinstall will try this slug again on the next session.
  };
  const by = typeof env.INTERNAL_USER_ID === "string" ? env.INTERNAL_USER_ID.trim() : "";
  if (by && by.length <= 256) entry.installedBy = by;
  const session =
    typeof env.AGENTCORE_RUNTIME_SESSION_ID === "string"
      ? env.AGENTCORE_RUNTIME_SESSION_ID.trim()
      : typeof env.RUNTIME_SESSION_ID === "string"
        ? env.RUNTIME_SESSION_ID.trim()
        : "";
  if (session && session.length <= 256) entry.installedSession = session;
  manifest.skills[validSlug] = entry;
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
 * Minimal environment for the clawhub child: PATH/HOME/locale/TMPDIR/XDG dirs plus clawhub's
 * own CLAWHUB_ / CLAWDHUB_ settings. No AWS credential variables — clawhub
 * only talks to the ClawHub registry.
 */
function clawhubEnv(base = process.env) {
  const env = {};
  for (const key of ["PATH", "HOME", "LANG", "LC_ALL", "NODE_PATH", "TMPDIR",
    // Set for the unprivileged agent uid, whose HOME (/root) is not writable.
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"]) {
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
 * `skillsDir`. Sequential (one ClawHub download at a time), never throws.
 *
 * Self-healing policy (so a persistently unresolvable entry can never
 * permanently brick activation):
 *   - Corrupt / wrong-shape manifests are rewritten as an empty manifest
 *     (the file is attacker/agent-writable; keeping it as-is would feed
 *     `corrupt: true` on every cold start forever).
 *   - Entries that fail slug/version validation are dropped from the
 *     persisted manifest.
 *   - Each failed reinstall increments a per-entry `failures` counter. After
 *     `MAX_CONSECUTIVE_FAILURES` consecutive failures the entry is marked
 *     `disabled: true` and skipped on future cold starts (summary.quarantined
 *     bumps once, `failed` does not); a successful reinstall clears both
 *     counters. `recordInstall` from the clawhub-manage skill also clears
 *     them, so the user can re-enable a quarantined skill by reinstalling.
 *
 * Version pinning and integrity:
 *   - An entry with a recorded `integrity` digest is re-hashed after the
 *     reinstall. On a mismatch the reinstalled directory is removed (this
 *     runs before the gateway starts, so OpenClaw never loads it) and the
 *     entry is quarantined straight away with `disabledReason: "integrity"`.
 *   - An entry without a digest (written before digests were recorded) is
 *     reinstalled as before and its digest is recorded now, so later cold
 *     starts verify it. An entry without a pinned version also gets the
 *     version clawhub reports for the reinstalled files, so it stops
 *     following the registry's latest release. Neither case blocks the
 *     user's existing skills; such entries are counted in `unverified`.
 *
 * Time bound: no new install starts once `totalBudgetMs` has elapsed; the
 * remaining entries are counted in `deferred` and retried at the next cold
 * start (their failure streak is not touched).
 *
 * Returns a summary suitable for logging; `failed` / `corrupt` / `invalid`
 * remain informational only — callers must not treat them as fatal.
 *
 * @param {object} opts
 * @param {string} [opts.manifestFile]
 * @param {string} [opts.skillsDir]
 * @param {(args: string[]) => Promise<{stdout: string, stderr: string}>} [opts.run]  injected for tests
 * @param {object} [opts.log]
 * @param {number} [opts.maxConsecutiveFailures]
 * @param {object} [opts.env]  source of the operator allowlist
 * @param {number} [opts.totalBudgetMs]
 * @param {() => number} [opts.now]  injected for tests
 * @returns {Promise<{ total: number, reinstalled: number, present: number, failed: number, deferred: number, unverified: number, quarantined: number, skipped: number, refused: number, corrupt: boolean, invalid: number, migrated: number, results: Array<{slug: string, version: string|null, status: string, error?: string, verified?: boolean}> }>}
 */
async function reinstallFromManifest({
  manifestFile = manifestPath(),
  skillsDir = SKILLS_DIR,
  run = runClawhub,
  log = console,
  maxConsecutiveFailures = MAX_CONSECUTIVE_FAILURES,
  env = process.env,
  totalBudgetMs = REINSTALL_TOTAL_BUDGET_MS,
  now = Date.now,
} = {}) {
  const summary = {
    total: 0, reinstalled: 0, present: 0, failed: 0, deferred: 0, unverified: 0,
    quarantined: 0, skipped: 0, corrupt: false, invalid: 0, refused: 0, migrated: 0, results: [],
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

  // Self-heal: rewrite a clean manifest if we found corruption or dropped
  // invalid entries, so the next cold start starts from a trustworthy state
  // and the same `corrupt`/`invalid` counters do not re-fire forever.
  if (manifest.corrupt) {
    try {
      writeManifest(manifestFile, emptyManifest());
      log.warn(`[runtime-skills] Rewrote corrupt manifest ${manifestFile} as empty`);
    } catch (err) {
      log.warn(`[runtime-skills] Could not rewrite corrupt manifest ${manifestFile}: ${err.message}`);
    }
    return summary;
  }
  if (manifest.invalid > 0) {
    try {
      writeManifest(manifestFile, { skills: manifest.skills });
      log.warn(`[runtime-skills] Rewrote manifest ${manifestFile} without ${manifest.invalid} invalid entr${manifest.invalid === 1 ? "y" : "ies"}`);
    } catch (err) {
      log.warn(`[runtime-skills] Could not rewrite manifest ${manifestFile}: ${err.message}`);
    }
  }

  const entries = Object.entries(manifest.skills);
  summary.total = entries.length;
  if (entries.length === 0) return summary;

  const allowlist = parseAllowlist(env, { log });
  if (allowlist) {
    log.log(`[runtime-skills] Operator allowlist active: ${allowlist.size} slug(s) permitted`);
  }

  const deadline = now() + totalBudgetMs;
  let manifestDirty = false;
  for (const [slug, entry] of entries) {
    const version = entry.version || null;
    const label = `${slug}${version ? `@${version}` : ""}`;
    if (!isAllowed(slug, allowlist)) {
      summary.refused++;
      summary.results.push({ slug, version, status: "refused", error: "not in operator allowlist" });
      log.warn(
        `[runtime-skills] Refusing to reinstall ${label}: not in ${ALLOWLIST_ENV}` +
          `${entry.installedBy ? ` (recorded installedBy=${entry.installedBy})` : " (no provenance)"}`,
      );
      continue;
    }
    if (entry.disabled === true) {
      summary.skipped++;
      summary.results.push({
        slug, version, status: "skipped",
        error: entry.disabledReason === "integrity"
          ? "quarantined: reinstalled files did not match the recorded checksum"
          : "quarantined after repeated failures",
      });
      continue;
    }
    if (isSkillPresent(slug, skillsDir)) {
      summary.present++;
      summary.results.push({ slug, version, status: "present" });
      if (entry.failures) {
        entry.failures = 0;
        manifestDirty = true;
      }
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
      const digests = computeSkillDigests(slug, skillsDir);
      const actualIntegrity = digests ? digests.current : null;
      const verified = !!entry.integrity;
      if (entry.integrity) {
        if (digests && digests.legacy === entry.integrity && digests.current !== entry.integrity) {
          // Recorded by an earlier build in the legacy digest format: the files
          // match, so re-record the digest in the current format.
          entry.integrity = digests.current;
          manifestDirty = true;
        } else if (actualIntegrity !== entry.integrity) {
          let removed = "removed";
          try {
            fs.rmSync(path.join(skillsDir, slug), { recursive: true, force: true });
          } catch (rmErr) {
            removed = `could NOT be removed (${rmErr.message})`;
          }
          // Retrying would fetch the same files, so quarantine straight away;
          // installing the skill again (recordInstall) lifts it.
          entry.failures = (entry.failures || 0) + 1;
          entry.disabled = true;
          entry.disabledReason = "integrity";
          manifestDirty = true;
          summary.quarantined++;
          const msg = `reinstalled files do not match the checksum recorded at install time (expected ${entry.integrity}, got ${actualIntegrity || "unreadable"}); ${removed}. Install the skill again to accept the current files.`;
          summary.results.push({ slug, version, status: "quarantined", error: msg });
          log.warn(`[runtime-skills] Quarantining ${label}: ${msg}`);
          continue;
        }
      } else {
        // Entry written before digests were recorded: reinstalled unverified
        // this time; record a digest now so later cold starts verify it, and
        // pin the version clawhub resolved if none was recorded.
        summary.unverified++;
        const recorded = [];
        if (actualIntegrity) {
          entry.integrity = actualIntegrity;
          recorded.push("integrity digest");
        }
        const resolved = version ? null : readInstalledVersion(slug, skillsDir);
        if (resolved) {
          entry.version = resolved;
          recorded.push(`version ${resolved}`);
        }
        if (recorded.length) {
          manifestDirty = true;
          summary.migrated++;
          log.log(`[runtime-skills] Recorded ${recorded.join(" and ")} for ${slug}`);
        }
      }
      summary.reinstalled++;
      summary.results.push({ slug, version: entry.version || null, status: "reinstalled", verified });
      const prov = entry.installedBy
        ? ` (installedBy=${entry.installedBy}${entry.installedAt ? ` at ${entry.installedAt}` : ""})`
        : " (no provenance recorded)";
      log.log(`[runtime-skills] Reinstalled ${label}${verified ? " (checksum verified)" : " (no recorded checksum)"} in ${now() - started}ms${prov}`);
      if (entry.failures) {
        entry.failures = 0;
        manifestDirty = true;
      }
    } catch (err) {
      const detail = String(err.stderr || err.stdout || err.message || err).trim().split("\n").slice(-3).join(" | ");
      const nextFailures = (entry.failures || 0) + 1;
      if (nextFailures >= maxConsecutiveFailures) {
        entry.failures = nextFailures;
        entry.disabled = true;
        entry.disabledReason = "failures";
        manifestDirty = true;
        summary.quarantined++;
        summary.results.push({ slug, version, status: "quarantined", error: detail });
        log.warn(`[runtime-skills] Quarantining ${label} after ${nextFailures} consecutive failures: ${detail}`);
      } else {
        entry.failures = nextFailures;
        manifestDirty = true;
        summary.failed++;
        summary.results.push({ slug, version, status: "failed", error: detail });
        log.warn(`[runtime-skills] Reinstall of ${label} failed after ${now() - started}ms (attempt ${nextFailures}/${maxConsecutiveFailures}): ${detail}`);
      }
    }
  }
  if (manifestDirty) {
    try {
      writeManifest(manifestFile, { skills: manifest.skills });
    } catch (err) {
      log.warn(`[runtime-skills] Could not persist reinstall state to ${manifestFile}: ${err.message}`);
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
  MAX_CONSECUTIVE_FAILURES,
  ALLOWLIST_ENV,
  isValidIntegrity,
  computeSkillIntegrity,
  parseAllowlist,
  isAllowed,
  validateSkillName,
  isValidSlug,
  isValidVersion,
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
