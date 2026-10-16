/**
 * AgentCore Runtime Contract Server — Per-User Sessions
 *
 * Implements the required HTTP protocol contract for AgentCore Runtime:
 *   - GET  /ping         -> Health check (Healthy — allows idle termination)
 *   - POST /invocations  -> Chat handler with per-user activation
 *
 * AgentCore Runtime V2 prepares the gateway without user resources,
 * then waits for restored state and verified resources before dispatch.
 *
 * Runs on port 8080 (required by AgentCore Runtime).
 */

if (process.env.AGENTCORE_RUNTIME_PLATFORM !== undefined && process.env.AGENTCORE_RUNTIME_PLATFORM !== "V2") {
  throw new Error("AGENTCORE_RUNTIME_PLATFORM must be V2 when set");
}

const http = require("http");
const fs = require("fs");
const { createHash } = require("crypto");
const { spawn, execFile } = require("child_process");
const {
  SecretsManagerClient,
  GetSecretValueCommand,
} = require("@aws-sdk/client-secrets-manager");
const workspaceSync = require("./workspace-sync");
const stateStorage = require("./state-storage");
const legacySessionImport = require("./legacy-session-import");
const cwLogger = require("./cloudwatch-logger");
const { createStartupTiming } = require("./startup-timing");
const startupTiming = createStartupTiming({
  env: process.env, emit: (record) => console.log(JSON.stringify(record)),
});
const RUNTIME_PLATFORM = "V2";
const scopedCreds = require("./scoped-credentials");
const gatewayMcp = require("./gateway-mcp");
const runtimeSkills = require("./runtime-skills");
const { createCognitoTokenProvider } = require("./cognito-token");
const { readBody } = require("./read-body");
const { createRuntimeLifecycle, osRandomUUID } = require("./runtime-lifecycle");
const { waitForGateway, gatewayTurn, dismissStaleSuspendedDeliveries } = require("./gateway-lifecycle");
const { createPreparedGateway } = require("./openclaw-worker-owner");
const { renderConfig, renderAgents } = require("./gateway-config");
const { createLiveSeedBridge, readGatewayToken } = require("./live-seed-bridge");
const { createProxySdkSnapshotOwner } = require("./proxy-sdk-snapshot");
const { createProxyAuthParent } = require("./proxy-auth-prepare");

const PORT = 8080;
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;

// Session storage mount path (set via filesystemConfigurations on AgentCore Runtime)
const SESSION_STORAGE_MOUNT = stateStorage.DEFAULT_MOUNT;
// OpenClaw state dir. Always a REAL directory on the container's local disk:
// OpenClaw 2.0 keeps sessions/auth/state in SQLite, and AgentCore session
// storage is NFS with local_lock=none, on which SQLite cannot take a lock
// ("database is locked" at gateway startup). Only ~/.openclaw/workspace is a
// real local directory (SQLite locks + hard links both work); state-storage.js
// mirrors it, workspace included, onto the mount.
const OPENCLAW_DIR = stateStorage.resolvePaths().stateDir;
// Cold mirror of OPENCLAW_DIR on the mount (also where a 1.x deployment left
// its plain-file state dir).
const MOUNTED_OPENCLAW_DIR = stateStorage.resolvePaths().mountStateDir;
// Max time the SIGTERM handler waits for the gateway to exit before taking the
// shutdown state snapshot (a closed database gives a fully quiesced copy; the
// snapshot is consistent either way via the SQLite online backup API). Kept
// short: on AgentCore the container was gone well under 5 s after SIGTERM
// (staging logs), and an idle gateway reaches "shutdown started" within ~50 ms.
const GATEWAY_STOP_WAIT_MS = parseInt(process.env.GATEWAY_STOP_WAIT_MS || "2000", 10);

// Gateway token — re-fetched during user activation after common seed preparation.
// No fallback — container will fail to authenticate WebSocket if not set.
let GATEWAY_TOKEN = null;

// Telegram bot token — fetched during user activation.
// Used for typing indicator during processing + single final message delivery.
let TELEGRAM_BOT_TOKEN = null;

// Cognito password secret — fetched during user activation.
// Stored in-process only, never written to process.env.
let COGNITO_PASSWORD_SECRET = null;

// Maximum request body size (1MB) to prevent memory exhaustion
const MAX_BODY_SIZE = 1 * 1024 * 1024;

// Ping diagnostics — track call count and log periodically
let pingCount = 0;
let lastPingLogTime = 0;
const PING_LOG_INTERVAL_MS = 60000; // Log ping stats every 60s

// State tracking
let currentUserId = null;
let currentNamespace = null;
let openclawProcess = null;
let preparedGateway = null;
let proxySdkOwner = null;
let proxyAuthControl = null;
let scopedLogger = null;
let proxyProcess = null;
let openclawReady = false;
let proxyReady = false;
let secretsReady = false;
let initInProgress = false;
let initPromise = null;
let startTime = Date.now();
let shuttingDown = false;
// True once setupSessionStorage() found the mount; gates the state mirror.
let sessionStorageActive = false;
// fs.watch handle that backs the runtime-skills manifest up to S3 on change.
let runtimeSkillManifestWatcher = null;
let browserHeaderRefreshTimer = null;
let credentialManager = null;
let gatewayTokenProvider = null;
let browserExpiresAt = 0;
let browserGeneration = 0;
let browserStartOwner = null;
const browserResourceOrigins = new Map();
const browserSessions = new Set();
const browserStops = new Map();
let activationScope = null;
let intentionalGatewayStop = null;
let migrationProcess = null;
let configPublishPromise = Promise.resolve();
let configPublishError = null;
const runtimeSkillChildren = new Set();
// AgentCore Gateway MCP tools (prototype). Only active when
// AGENTCORE_GATEWAY_URL is set; otherwise openclaw.json is unchanged.
const AGENTCORE_GATEWAY_URL = (process.env.AGENTCORE_GATEWAY_URL || "").trim();
let gatewayBearer = null; // { token, expiresAt } for the current user
let currentBrowserSessionId = null;
let currentBrowserEndpoint = null;
const SCOPED_CREDS_DIR = "/tmp/scoped-creds";
const IDENTITY_FILE = "/tmp/current-identity.json";
const BROWSER_SESSION_FILE = "/tmp/agentcore-browser-session.json";
const BROWSER_SESSION_TIMEOUT_SECONDS = 3600;
const BUILD_VERSION = "v40"; // Bump in cdk.json to force container redeploy
const ACTIVATION_TIMEOUT_MS = Number(process.env.USER_ACTIVATION_TIMEOUT_MS || 300000);
const TURN_TIMEOUT_MS = 620000;
// A cron caller (the cron Lambda) sends callerBudgetMs: how long it will keep
// reading this response. Briefs on one session queue behind each other, so a
// queued turn gets its own TURN_TIMEOUT_MS from dispatch, but never more than
// the caller's budget less a margin for unknown-outcome cleanup and the reply.
const MAX_CALLER_BUDGET_MS = 900000; // Lambda's maximum timeout
const CALLER_REPLY_MARGIN_MS = 20000;
let proxyModelOutcomeUnknown = false;
const UNKNOWN_OUTCOME_RESPONSE = "The request outcome is unknown. It has not been automatically retried. Check any resulting changes before retrying.";
const v2Lifecycle = createRuntimeLifecycle({
  bootstrap: commonBootstrap,
  activate: (context, scope) => {
    activationScope = scope;
    return init(context.userId, context.actorId, context.channel || "unknown",
      context.channelActorId, context.channelTarget);
  },
  cleanup: cleanupV2Resources,
  activationTimeoutMs: ACTIVATION_TIMEOUT_MS,
});

// Derive EVENTBRIDGE_ROLE_ARN from EXECUTION_ROLE_ARN + AWS_REGION if not already set.
// The agentcore toolkit doesn't support injecting arbitrary env vars into the container,
// so we derive it: arn:aws:iam::{account}:role/openclaw-cron-scheduler-role-{region}
if (!process.env.EVENTBRIDGE_ROLE_ARN && process.env.EXECUTION_ROLE_ARN) {
  const match = process.env.EXECUTION_ROLE_ARN.match(/arn:aws:iam::(\d+):role\//);
  if (match) {
    const account = match[1];
    const region = process.env.AWS_REGION || "us-west-2";
    process.env.EVENTBRIDGE_ROLE_ARN = `arn:aws:iam::${account}:role/openclaw-cron-scheduler-role-${region}`;
    console.log(`[contract] Derived EVENTBRIDGE_ROLE_ARN from execution role (account=${account}, region=${region})`);
  }
}

// OpenClaw process diagnostics (last N lines of stdout/stderr)
const OPENCLAW_LOG_LIMIT = 50;
let openclawLogs = [];
let openclawExitCode = null;
let lastOpenClawEnv = null;

// V2 fails activation if the strict restore barrier exceeds this deadline.
const RESTORE_WAIT_MS = parseInt(process.env.WORKSPACE_RESTORE_WAIT_MS || "45000", 10);
// Max time allowed for the one-time legacy sessions.json -> SQLite import.
const LEGACY_MIGRATION_TIMEOUT_MS = parseInt(process.env.OPENCLAW_MIGRATION_TIMEOUT_MS || "180000", 10);

// Active task tracking — HealthyBusy prevents AgentCore from terminating during long tasks
let activeTaskCount = 0;
// Last activity timestamp (epoch seconds) — reported in /ping so AgentCore can track idle time.
// Initialized to startup time; updated on each chat/cron/warmup invocation.
let lastActivityTime = Math.floor(Date.now() / 1000);

async function commonBootstrap() {
  if (!Number.isFinite(ACTIVATION_TIMEOUT_MS) || ACTIVATION_TIMEOUT_MS <= 0) {
    throw new Error("Invalid USER_ACTIVATION_TIMEOUT_MS");
  }
  // Validate immutable inputs before preparing code in the serving child.
  fs.readFileSync(`${__dirname}/CLAUDE.md`, "utf8");
  const pin = JSON.parse(fs.readFileSync("/usr/local/lib/node_modules/openclaw/package.json", "utf8"));
  if (pin.version !== "2026.9.5") throw new Error("Unexpected OpenClaw version");
  for (const skill of ["jina-reader", "deep-research-pro", "telegram-compose", "transcript",
    "task-decomposer", "s3-user-files", "eventbridge-cron", "clawhub-manage", "api-keys",
    "agentcore-browser"]) {
    fs.readFileSync(`/skills/${skill}/SKILL.md`, "utf8");
  }
  assertUnboundResourcesAbsent();
  const env = scopedCreds.buildOpenClawEnv({ credDir: SCOPED_CREDS_DIR, baseEnv: process.env });
  Object.assign(env, preparedGatewayStateEnv());
  delete env.USER_ID;
  delete env.INTERNAL_USER_ID;
  const createCold = () => createPreparedGateway({
    env, port: OPENCLAW_PORT,
    onChild: observeGatewayChild,
    onFailure: (error) => {
      const child = preparedGateway?.child;
      if (shuttingDown || (child && (intentionalGatewayStop === child || openclawProcess !== child))) return;
      v2Lifecycle.fail(error);
    },
  });
  preparedGateway = createLiveSeedBridge({
    createOwner: async (options) => (await import("./live-gateway-seed-owner.mjs"))
      .createLiveGatewaySeedOwner({ ...options, onEvent: (event) => {
        if (["prepared", "bound", "stopped"].includes(event.event)) {
          console.log("[gateway] " + JSON.stringify({ stage: event.event,
            gateway_pid: event.pid, seed_id: event.seedId,
            generation: v2Lifecycle.snapshot().generation }));
        }
      } }),
    createCold, render: (token) => ({ config: renderConfig({ gatewayToken: token }),
      agentsText: renderAgents({ browserEnabled: !!process.env.BROWSER_IDENTIFIER }) }),
    stateStorage, workspaceSync, neutralEnv: env,
    readToken: () => readGatewayToken({ SecretsManagerClient, GetSecretValueCommand }),
    onChild: observeGatewayChild,
    onDiscard: (child) => { if (child) intentionalGatewayStop = child; },
    inspectLocalEligibility: () => findLegacySessionStores().length === 0 &&
      !fs.existsSync(runtimeSkills.manifestPath(OPENCLAW_DIR)) &&
      !fs.existsSync(`${OPENCLAW_DIR}/.pre-2.0-retired-files.json`),
    port: OPENCLAW_PORT,
  });
  // Imports only: clients and credential resolution remain behind user binding.
  require("@aws-sdk/client-bedrock-agentcore");
  require("@smithy/signature-v4");
  require("@aws-crypto/sha256-js");
  require("@aws-sdk/credential-provider-node");
  require("@aws-sdk/client-cognito-identity-provider");
  proxySdkOwner = createProxySdkSnapshotOwner({
    env: process.env, onChild: observeProxyChild,
    onFailure: (error) => { if (!shuttingDown) void v2Lifecycle.fail(error); },
  });
  const preparationStarted = Date.now();
  await proxySdkOwner.prepare();
  v2Lifecycle.checkpoint();
  assertUnboundResourcesAbsent();
  await preparedGateway.prepare();
  v2Lifecycle.checkpoint();
  assertUnboundResourcesAbsent(preparedGateway.prepared);
  console.log(`[contract] preparation elapsedMs=${Date.now() - preparationStarted}`);
  console.log("[contract] BOOTSTRAP_READY platform=V2 openclaw=2026.9.5");
}

function assertUnboundResourcesAbsent(seedPrepared = false) {
  for (const path of [IDENTITY_FILE, BROWSER_SESSION_FILE, `${SCOPED_CREDS_DIR}/scoped-creds.json`,
    `${SCOPED_CREDS_DIR}/scoped-aws-config`]) {
    if (fs.existsSync(path)) throw new Error("User resource present before bootstrap");
  }
  if (!seedPrepared && fs.existsSync(OPENCLAW_DIR) && fs.readdirSync(OPENCLAW_DIR).length) {
    throw new Error("User state present before bootstrap");
  }
  if (process.env.USER_ID || process.env.INTERNAL_USER_ID) throw new Error("User binding present before bootstrap");
  if (seedPrepared) preparedGateway.assertFiles();
}

async function cleanupV2Resources() {
  const sdkStop = proxySdkOwner?.stop();
  sdkStop?.catch(() => {});
  retireBrowserStartOwner().catch(() => {});
  scopedLogger?.retire();
  proxyAuthControl?.stop();
  proxyAuthControl = null;
  openclawReady = false;
  proxyReady = false;
  const ownerCredentials = credentialManager;
  ownerCredentials?.beginDrain();
  const stopCredentials = () => {
    // SIGTERM takes over persistence ownership, including its shutdown deadline.
    if (!shuttingDown) ownerCredentials?.stop();
  };
  const cleanupDeadline = shuttingDown ? null : setTimeout(stopCredentials, 10000);
  try {
    clearInterval(browserHeaderRefreshTimer);
    browserHeaderRefreshTimer = null;
    runtimeSkillManifestWatcher?.close();
    runtimeSkillManifestWatcher = null;
    stateStorage.stopPeriodicMirror();
    stateStorage.stopWorkspaceWatcher();
    const urgentSave = shuttingDown ? Promise.resolve()
      : workspaceSync.flushPendingSaves("failure", { force: true });
    void preparedGateway?.stop(shuttingDown ? "shutdown" : "abort").catch(() => {});
    const children = [openclawProcess, proxyProcess, migrationProcess, ...runtimeSkillChildren]
      .filter((child) => child && child !== proxySdkOwner?.child);
    await Promise.all(children.map((child) => new Promise((resolve) => {
      if (child.exitCode !== null || child.signalCode !== null) return resolve();
      const timer = setTimeout(() => {
        try { child.kill("SIGKILL"); } catch {}
        resolve();
      }, GATEWAY_STOP_WAIT_MS);
      child.once("exit", () => { clearTimeout(timer); resolve(); });
      try { child.kill("SIGTERM"); } catch { clearTimeout(timer); resolve(); }
    })));
    if (sdkStop) await sdkStop;
    await urgentSave;
    // Include the gateway's shutdown writes before retiring persistence credentials.
    if (!shuttingDown) await workspaceSync.cleanup(currentNamespace);
    await Promise.allSettled([stopBrowserSessions(), cwLogger.shutdown()]);
  } finally {
    clearTimeout(cleanupDeadline);
    stopCredentials();
  }
}

function runRuntimeSkillInstall(args) {
  activationScope.checkpoint();
  return new Promise((resolve, reject) => {
    const child = execFile("clawhub", args, {
      encoding: "utf8", timeout: runtimeSkills.REINSTALL_TIMEOUT_MS,
      env: runtimeSkills.clawhubEnv(), signal: activationScope.signal,
      maxBuffer: 4 * 1024 * 1024,
    }, (error, stdout, stderr) => {
      runtimeSkillChildren.delete(child);
      if (error) {
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      } else {
        resolve({ stdout, stderr });
      }
    });
    runtimeSkillChildren.add(child);
  });
}

function startBoundGateway(env) {
  const child = spawn("openclaw",
    ["gateway", "run", "--port", String(OPENCLAW_PORT), "--verbose"],
    { stdio: ["ignore", "pipe", "pipe"], env });
  lastOpenClawEnv = env;
  observeGatewayChild(child);
}

function observeGatewayChild(child) {
  openclawProcess = child;
  openclawExitCode = null;
  for (const [stream, label] of [[child.stdout, "out"], [child.stderr, "err"]]) {
    let buffer = "";
    stream.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines.filter((value) => value.trim())) {
        console.log(`[openclaw:${label}] ${line}`);
        openclawLogs.push(`[${label}] ${line}`);
        if (openclawLogs.length > OPENCLAW_LOG_LIMIT) openclawLogs.shift();
      }
    });
  }
  child.on("error", (error) => {
    v2Lifecycle.fail(error);
  });
  child.on("exit", (code) => {
    if (openclawProcess !== child) return;
    openclawExitCode = code;
    openclawReady = false;
    if (intentionalGatewayStop === child || shuttingDown) return;
    v2Lifecycle.fail(new Error("Bound gateway exited"));
  });
}

function observeProxyChild(child) {
  proxyProcess = child;
  child.stdout.on("data", (data) => {
    data.toString().split("\n").filter(Boolean).forEach((line) => console.log(`[proxy:out] ${line}`));
  });
  child.stderr.on("data", (data) => {
    data.toString().split("\n").filter(Boolean).forEach((line) => console.error(`[proxy:err] ${line}`));
  });
}

async function verifyV2Resources(context) {
  activationScope.checkpoint();
  workspaceSync.assertRestoreReady();
  await credentialManager.ensureFresh();
  activationScope.checkpoint();
  if (AGENTCORE_GATEWAY_URL && gatewayBearer.expiresAt <= Date.now() + TURN_TIMEOUT_MS + 60000) {
    gatewayBearer = await gatewayTokenProvider.getAccessToken(context.actorId, { force: true });
    activationScope.checkpoint();
    if (gatewayBearer.expiresAt <= Date.now() + TURN_TIMEOUT_MS + 60000) {
      throw new Error("Gateway bearer validity is too short for a turn");
    }
    // Config reload alone does not prove that MCP adopted a refreshed bearer.
    intentionalGatewayStop = openclawProcess;
    try {
      if (!await stopGateway(GATEWAY_STOP_WAIT_MS)) throw new Error("Gateway did not stop for bearer refresh");
      writeOpenClawConfig();
      await configPublishPromise;
      if (configPublishError) throw configPublishError;
      activationScope.checkpoint();
      startBoundGateway(lastOpenClawEnv);
    } finally {
      intentionalGatewayStop = null;
    }
  }
  await ensureV2BrowserSession(context.userId, context.deadline);
  await assertProxyOutcomeKnown();
  await waitForGateway({
    port: OPENCLAW_PORT, token: GATEWAY_TOKEN, signal: activationScope.signal,
    deadline: Math.min(context.deadline, Date.now() + 30000), configProbe: true,
  });
  activationScope.checkpoint();
  await proxySdkOwner.attest(v2Lifecycle.snapshot().binding, activationScope.signal);
  activationScope.checkpoint();
  v2Lifecycle.assertBinding(context);
  openclawReady = true;
}

/**
 * Write current actorId and channel to a shared file so the proxy process
 * can pick up cross-channel identity changes (the proxy's env vars are
 * fixed at spawn time and cannot be updated for a running child process).
 */
/**
 * Set up the state storage layout (see state-storage.js): a real local
 * ~/.openclaw including ~/.openclaw/workspace (the NFS session storage mount
 * supports neither SQLite locks nor the hard links OpenClaw 2.0 uses to
 * publish workspace files), and the mount's mirror (plain files + workspace +
 * SQLite snapshots, or a 1.x plain-file state dir) restored to local disk.
 * Must run before anything touches ~/.openclaw and before the gateway spawns.
 * Returns true if session storage is available.
 */
function setupSessionStorage() {
  const result = stateStorage.setupSessionStorage({ log: console });
  if (!result.available) {
    console.log("[contract] Session storage not available at", SESSION_STORAGE_MOUNT);
    return false;
  }
  return true;
}

/**
 * Snapshot the local state dir onto the session storage mount. Coalesced,
 * never throws. Cheap (loopback NFS), so it is called at shutdown and on a
 * timer while the gateway runs.
 */
async function mirrorStateToSessionStorage(label) {
  if (!sessionStorageActive) return null;
  try {
    const r = await stateStorage.mirrorNow({
      stateDir: OPENCLAW_DIR,
      mountStateDir: MOUNTED_OPENCLAW_DIR,
      log: console,
      assertReady: workspaceSync.assertRestoreReady,
    });
    console.log(
      `[contract] State mirror (${label}): ${r.files} file(s) copied, ${r.unchanged} unchanged, ${r.sqlite} sqlite snapshot(s), ${r.skipped} skipped, ${r.pruned} pruned`,
    );
    return r;
  } catch (err) {
    console.warn(`[contract] State mirror (${label}) failed: ${err.message}`);
    return null;
  }
}

/**
 * Ask the gateway to exit and wait (bounded) for it, so the shutdown state
 * snapshot is taken from a closed, checkpointed database.
 */
function stopGateway(waitMs) {
  return new Promise((resolve) => {
    const proc = openclawProcess;
    if (!proc || proc.exitCode !== null || proc.signalCode !== null) return resolve(false);
    const timer = setTimeout(() => resolve(false), waitMs);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
    try {
      proc.kill("SIGTERM");
    } catch {
      clearTimeout(timer);
      resolve(false);
    }
  });
}

function updateIdentityFile(actorId, channel, channelActorId = null, channelTarget = null,
  modelOverride = null, { trigger = "chat", sessionId = null } = {}) {
  const tempFile = `${IDENTITY_FILE}.${osRandomUUID()}.tmp`;
  try {
    // modelOverride is per turn: a turn without one (Cron, or no /model choice)
    // rewrites the file without it, so the proxy uses the deployment default.
    // trigger (chat | cron) and sessionId (the AgentCore runtime session) are
    // per turn too: the proxy puts them in Bedrock requestMetadata so the
    // token_metrics Lambda can attribute usage.
    fs.writeFileSync(
      tempFile,
      JSON.stringify({ actorId, channel, channelActorId, channelTarget,
        ...(modelOverride ? { modelOverride } : {}),
        trigger: trigger === "cron" ? "cron" : "chat",
        ...(typeof sessionId === "string" && sessionId ? { sessionId } : {}) }),
      "utf-8",
    );
    fs.renameSync(tempFile, IDENTITY_FILE);
  } finally {
    try { fs.unlinkSync(tempFile); } catch {}
  }
}

/**
 * Fetch secrets during user activation.
 */
async function prefetchSecrets() {
  const region = process.env.AWS_REGION || "us-west-2";
  const smClient = new SecretsManagerClient({ region });

  const gatewaySecretId = process.env.GATEWAY_TOKEN_SECRET_ID;
  if (gatewaySecretId) {
    const resp = await smClient.send(
      new GetSecretValueCommand({ SecretId: gatewaySecretId }),
    );
    if (resp.SecretString) {
      GATEWAY_TOKEN = resp.SecretString;
      console.log("[contract] Gateway token pre-fetched from Secrets Manager");
    }
  }

  const cognitoSecretId = process.env.COGNITO_PASSWORD_SECRET_ID;
  if (cognitoSecretId) {
    const resp = await smClient.send(
      new GetSecretValueCommand({ SecretId: cognitoSecretId }),
    );
    if (resp.SecretString) {
      COGNITO_PASSWORD_SECRET = resp.SecretString;
      console.log("[contract] Cognito password secret pre-fetched");
    }
  }

  const telegramSecretId = process.env.TELEGRAM_CHANNEL_SECRET_ID;
  if (telegramSecretId) {
    try {
      const resp = await smClient.send(
        new GetSecretValueCommand({ SecretId: telegramSecretId }),
      );
      if (resp.SecretString) {
        // Secret may be a plain token or JSON with bot_token/token key
        try {
          const parsed = JSON.parse(resp.SecretString);
          TELEGRAM_BOT_TOKEN =
            parsed.bot_token || parsed.token || resp.SecretString;
        } catch {
          TELEGRAM_BOT_TOKEN = resp.SecretString;
        }
        console.log(
          "[contract] Telegram bot token pre-fetched from Secrets Manager",
        );
      }
    } catch (err) {
      console.warn(
        `[contract] Telegram secret fetch failed (streaming disabled): ${err.message}`,
      );
    }
  }

  secretsReady = true;
  console.log("[contract] Secrets pre-fetch complete");
}

/**
 * Clean up stale .lock files in the .openclaw directory (async, non-blocking).
 * Prevents "session file locked" errors after workspace restore from S3.
 */
async function cleanupLockFiles() {
  const fs = require("fs");
  const path = require("path");
  const homeDir = process.env.HOME || "/root";
  const openclawDir = path.join(homeDir, ".openclaw");

  try {
    await fs.promises.access(openclawDir);
  } catch {
    return; // Directory doesn't exist yet — nothing to clean
  }

  async function walkAndClean(dir) {
    let entries;
    try {
      entries = await fs.promises.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    const tasks = [];
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        tasks.push(walkAndClean(fullPath));
      } else if (entry.name.endsWith(".lock")) {
        tasks.push(
          fs.promises.unlink(fullPath).catch(() => {}),
        );
      }
    }
    await Promise.all(tasks);
  }

  await walkAndClean(openclawDir);
  console.log("[contract] Lock file cleanup complete (async)");
}

/**
 * Pin every selector captured by module imports before the child is prepared.
 */
function preparedGatewayStateEnv() {
  return {
    ...stateStorage.gatewayStateEnv(OPENCLAW_DIR),
    HOME: process.env.HOME || "/root",
    OPENCLAW_CONFIG_PATH: `${OPENCLAW_DIR}/openclaw.json`,
    OPENCLAW_WORKSPACE_DIR: `${OPENCLAW_DIR}/workspace`,
  };
}

/**
 * Build the environment OpenClaw processes run with.
 * Excludes container credentials and uses credential_process for scoped S3
 * access only.
 */
function openclawEnvFor(userId) {
  const openclawEnv = scopedCreds.buildOpenClawEnv({
    credDir: SCOPED_CREDS_DIR,
    baseEnv: process.env,
  });
  // Propagate INTERNAL_USER_ID so OpenClaw skills (e.g., eventbridge-cron)
  // can resolve the container's authorized userId for DynamoDB writes.
  openclawEnv.INTERNAL_USER_ID = userId;
  // Keep SQLite and workspace on local disk; session storage holds their cold mirror.
  Object.assign(openclawEnv, preparedGatewayStateEnv());
  return openclawEnv;
}

/**
 * Find pre-2.0 (file-backed) session stores in the state dir.
 * OpenClaw <= 2026.7.x kept `~/.openclaw/agents/<id>/sessions/sessions.json`;
 * 2.0 keeps rows in `~/.openclaw/agents/<id>/agent/openclaw-agent.sqlite`.
 * Returns the list of legacy index paths found (empty when already migrated).
 */
function findLegacySessionStores(openclawDir = OPENCLAW_DIR) {
  return legacySessionImport.findLegacySessionStores(openclawDir);
}

/**
 * Import a pre-2.0 sessions.json store into OpenClaw 2.0's SQLite store.
 *
 * 2.0's gateway does NOT migrate on its own: "If startup finds a legacy store,
 * it refuses readiness and prints the Doctor command" (docs/concepts/session.md).
 * Without this step an upgraded user with restored history would never get a
 * ready gateway. Runs `openclaw doctor --fix --non-interactive` once, bounded
 * by LEGACY_MIGRATION_TIMEOUT_MS, with the same scoped env as the gateway.
 *
 * An incomplete import fails activation and preserves the remaining files
 * for recovery.
 *
 * Runs once per index: S3 keeps the 1.x files (no deletes are propagated, and
 * they are the rollback path), so every cold start restores `sessions.json`
 * again next to the 2.0 SQLite store that already holds the imported history.
 * A receipt written after a successful import (legacy-session-import.js, the
 * sha256 of the imported index next to the store) lets later boots recognise
 * that index and move it aside instead of re-running doctor (~53 s for the
 * live user) and re-writing imported entries over 2.0's. An index with no or a
 * non-matching receipt, or whose store is missing, is imported as before.
 *
 * Doctor also retires the OTHER pre-2.0 state files (workspace-state.json,
 * auth-profiles.json, exec-approvals.json, the imported .jsonl transcripts, …)
 * and the 2.0 gateway refuses to run (exit 78, maintenance_required) while some
 * of them exist. Those come back from S3 on every later restore too, so on a
 * successful import the contract records which files doctor retired (path +
 * sha256, `.pre-2.0-retired-files.json` at the state-dir root) and
 * pruneResurrectedLegacyFiles() removes byte-identical copies after every
 * restore. `forceDoctor` runs doctor even without a legacy index — used when a
 * recorded file came back with DIFFERENT bytes (1.x rewrote it during a
 * rollback), which needs migrating again rather than deleting.
 *
 * @returns {Promise<boolean>} true when a legacy store was found (or doctor was
 *   forced) and doctor was run (the caller re-emits openclaw.json since doctor
 *   may rewrite it); false when there was nothing to migrate (or everything
 *   present had already been imported).
 */
async function migrateLegacySessionStore(env, { forceDoctor = false } = {}) {
  const plan = legacySessionImport.planLegacyImport(OPENCLAW_DIR);
  for (const item of plan.alreadyImported) {
    if (legacySessionImport.moveImportedIndexAside(item)) {
      console.log(
        `[contract] Pre-2.0 session store ${item.legacyIndex} was already imported into ${item.sqliteStore} (receipt matches) — moved aside, skipping doctor`,
      );
    } else {
      plan.toImport.push(item.legacyIndex);
    }
  }
  const legacy = plan.toImport;
  if (legacy.length === 0 && !forceDoctor) return false; // nothing to migrate

  if (legacy.length > 0) {
    console.log(
      `[contract] Pre-2.0 session store found (${legacy.join(", ")}) — running openclaw doctor --fix to import into SQLite`,
    );
  } else {
    console.log("[contract] Running openclaw doctor --fix to re-migrate changed pre-2.0 state files");
  }
  // Hash the indexes now: doctor archives them away once imported.
  const fingerprints = legacySessionImport.fingerprintIndexes(legacy);
  // And fingerprint the whole state dir: the files that are gone after doctor
  // are the ones every later restore must not resurrect (see the module).
  const fingerprintStarted = Date.now();
  const stateBeforeDoctor = legacySessionImport.fingerprintStateDir(OPENCLAW_DIR);
  console.log(
    `[contract] Fingerprinted ${stateBeforeDoctor.size} state file(s) before doctor in ${Date.now() - fingerprintStarted}ms`,
  );
  const started = Date.now();
  const result = await new Promise((resolve) => {
    let out = "";
    let settled = false;
    const finish = (r) => {
      if (!settled) {
        settled = true;
        resolve(r);
      }
    };
    let child;
    try {
      child = spawn(
        "openclaw",
        ["doctor", "--fix", "--non-interactive"],
        { stdio: ["ignore", "pipe", "pipe"], env },
      );
      migrationProcess = child;
    } catch (err) {
      return finish({ code: null, error: err.message, out: "" });
    }
    const timer = setTimeout(() => {
      try { child.kill("SIGKILL"); } catch {}
      finish({ code: null, error: `timed out after ${LEGACY_MIGRATION_TIMEOUT_MS}ms`, out });
    }, LEGACY_MIGRATION_TIMEOUT_MS);
    const capture = (chunk) => {
      out += chunk.toString();
      if (out.length > 64 * 1024) out = out.slice(-64 * 1024);
    };
    child.stdout.on("data", capture);
    child.stderr.on("data", capture);
    child.on("error", (err) => {
      clearTimeout(timer);
      finish({ code: null, error: err.message, out });
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      finish({ code, error: null, out });
    });
  });
  migrationProcess = null;
  activationScope.checkpoint();

  for (const line of result.out.split("\n").filter((l) => l.trim())) {
    console.log(`[openclaw:doctor] ${line}`);
  }

  const remaining = findLegacySessionStores();
  if (result.code === 0 && remaining.length === 0) {
    console.log(
      `[contract] Legacy session store imported into SQLite in ${Date.now() - started}ms`,
    );
    const receipts = legacySessionImport.writeReceipts(OPENCLAW_DIR, fingerprints);
    if (receipts.length) {
      console.log(`[contract] Recorded import receipt(s): ${receipts.join(", ")} — later cold starts skip this import`);
    }
    const retired = legacySessionImport.diffRetiredFiles(stateBeforeDoctor, OPENCLAW_DIR);
    const manifest = legacySessionImport.writeRetiredManifest(OPENCLAW_DIR, retired);
    if (manifest) {
      console.log(
        `[contract] Recorded ${retired.length} file(s) retired by doctor (${manifest.files.length} total) in ${manifest.path} — later restores remove them again`,
      );
    }
    // Both records are only useful once they are in S3, and nothing else
    // uploads them in time: this runs before gateway activation, so the change
    // watcher (started on gateway readiness) never sees the writes, the
    // periodic save is 30 min away in backup mode, and an idle stop sends no
    // SIGTERM. On us-west-2 staging (F6) two full upgrade boots wrote both files
    // and neither ever reached S3 — every later cold start re-ran doctor and the
    // retired-files prune had no manifest to work from.
    await backupImportRecords([...receipts, ...(manifest ? [manifest.path] : [])]);
    return true; // doctor ran (and succeeded)
  }

  throw new Error("Legacy session import did not complete");
}

/**
 * Upload the import receipt(s) / retired-files manifest written by
 * migrateLegacySessionStore() to S3 right away, one file at a time through the
 * same path the runtime-skills manifest uses (workspaceSync.saveFile: skip
 * rules, sha256 metadata, upload gate). Paths are absolute, inside OPENCLAW_DIR.
 * Never throws: a failed upload is logged and the periodic/final save retries.
 */
async function backupImportRecords(absolutePaths) {
  const namespace = currentNamespace;
  if (!namespace) return 0;
  let saved = 0;
  for (const abs of absolutePaths) {
    const prefix = OPENCLAW_DIR.endsWith("/") ? OPENCLAW_DIR : `${OPENCLAW_DIR}/`;
    if (!abs.startsWith(prefix)) continue;
    const rel = abs.slice(prefix.length);
    if (!rel) continue;
    try {
      if (await workspaceSync.saveFile(namespace, rel)) saved++;
    } catch (err) {
      console.warn(`[contract] Could not back up ${rel} to S3 now (${err.message}) — the periodic save will retry`);
    }
  }
  if (saved) console.log(`[contract] Backed up ${saved} import record(s) to S3`);
  return saved;
}

/**
 * Remove the pre-2.0 files a restore brought back that `openclaw doctor --fix`
 * had retired on the upgrade boot, when their bytes still match the record
 * (legacy-session-import.js, `.pre-2.0-retired-files.json`). Returns the
 * module's result; `changed` non-empty means a recorded file came back with
 * different bytes and the caller should run doctor again.
 */
function pruneResurrectedLegacyFiles() {
  const result = legacySessionImport.pruneRetiredFiles(OPENCLAW_DIR);
  if (result.pruned.length) {
    console.log(
      `[contract] Removed ${result.pruned.length} restored pre-2.0 file(s) that doctor had already retired (S3 keeps them for a rollback)`,
    );
  }
  if (result.changed.length) {
    console.warn(
      `[contract] ${result.changed.length} retired pre-2.0 file(s) came back with different bytes (${result.changed.slice(0, 5).join(", ")}${result.changed.length > 5 ? ", …" : ""}) — running doctor again`,
    );
  }
  return result;
}

/**
 * Check if the proxy health endpoint responds.
 */
function checkProxyHealth() {
  return new Promise((resolve) => {
    let settled = false;
    let timer;
    const finish = (health) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(health);
    };
    const req = http.get(`http://127.0.0.1:${PROXY_PORT}/health`, (res) => {
      let body = "";
      res.on("data", (chunk) => {
        body += chunk;
        if (Buffer.byteLength(body) > 65536) { finish(null); req.destroy(); }
      });
      res.on("error", () => finish(null));
      res.on("aborted", () => finish(null));
      res.on("end", () => {
        try {
          const health = JSON.parse(body);
          finish(res.statusCode === 200 && health.status === "ok" ? health : null);
        } catch {
          finish(null);
        }
      });
    });
    req.on("error", () => finish(null));
    const timeout = () => {
      finish(null);
      req.destroy();
    };
    timer = setTimeout(timeout, 2000);
    req.setTimeout(2000, timeout);
  });
}

async function assertProxyOutcomeKnown() {
  const health = await checkProxyHealth().catch(() => null);
  if (health?.model_outcome_unknown !== false) {
    proxyModelOutcomeUnknown = true;
    throw new Error("Proxy model outcome is unknown");
  }
}

/**
 * Wait for a port to become available, with timeout.
 */
async function waitForPort(port, label, timeoutMs = 300000, intervalMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const ready = await new Promise((resolve) => {
      const req = http.get(`http://127.0.0.1:${port}`, (res) => {
        res.resume();
        resolve(true);
      });
      req.on("error", () => resolve(false));
      req.setTimeout(2000, () => {
        req.destroy();
        resolve(false);
      });
    });
    if (ready) {
      console.log(`[contract] ${label} is ready on port ${port}`);
      return true;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  console.error(
    `[contract] ${label} did not become ready within ${timeoutMs / 1000}s`,
  );
  return false;
}

// Distinct subagent model name — proxy uses this to detect and route subagent requests.
// Must match the SUBAGENT_MODEL_NAME env var passed to the proxy.
const SUBAGENT_MODEL_NAME = "bedrock-agentcore-subagent";

/**
 * Write a headless OpenClaw config (no channels — messages bridged via WebSocket).
 * Full tool profile with deny list for unsafe/irrelevant tools.
 * Sub-agents enabled for deep-research-pro and task-decomposer skills.
 * Sandbox disabled — AgentCore microVMs provide per-user isolation.
 */
function writeOpenClawConfig({ preserveSeedFiles = false } = {}) {
  activationScope.checkpoint();
  workspaceSync.assertRestoreReady();
  const fs = require("fs");

  const config = renderConfig({ gatewayToken: GATEWAY_TOKEN });

  // AgentCore Gateway MCP tools (prototype): add mcp.servers.agentcore with
  // the per-user Cognito bearer when AGENTCORE_GATEWAY_URL is set. With the
  // env var unset `applyGatewayMcp` returns `config` untouched.
  const finalConfig = gatewayMcp.applyGatewayMcp(config, {
    gatewayUrl: AGENTCORE_GATEWAY_URL,
    token: gatewayBearer ? gatewayBearer.token : null,
  });

  const homeDir = process.env.HOME || "/root";
  if (preserveSeedFiles) preparedGateway.assertFiles();
  else {
    fs.mkdirSync(`${homeDir}/.openclaw`, { recursive: true });
    fs.writeFileSync(
      `${homeDir}/.openclaw/openclaw.json`,
      JSON.stringify(finalConfig, null, 2),
    );
  }
  console.log(
    `[contract] OpenClaw headless config written${finalConfig.mcp ? " (mcp.servers.agentcore enabled)" : ""}`,
  );

  // Write AGENTS.md — OpenClaw loads this as workspace bootstrap instructions.
  // Restored workspaces may carry stale instructions. An adopted seed was
  // already checked against this exact template.
  const agentsMdPath = `${homeDir}/.openclaw/AGENTS.md`;
  {
    if (!preserveSeedFiles) {
      fs.writeFileSync(
        agentsMdPath,
        renderAgents({ browserEnabled: !!process.env.BROWSER_IDENTIFIER }),
      );
      console.log("[contract] AGENTS.md written");
    }

    // Also upload to S3 so the proxy's system prompt injection picks it up.
    // The proxy reads AGENTS.md from S3 (not local filesystem), so without
    // this sync the proxy would inject the stale 156-byte default template.
    const namespace = currentNamespace;
    const bucket = process.env.S3_USER_FILES_BUCKET;
    if (bucket && namespace) {
      const agentsContent = fs.readFileSync(agentsMdPath, "utf-8");
      const publish = (async () => {
        try {
          const { PutObjectCommand } = require("@aws-sdk/client-s3");
          // Use the bound persistence client for the proxy's system prompt.
          const s3 = workspaceSync.getS3Client();
          await s3.send(new PutObjectCommand({
            Bucket: bucket,
            Key: `${namespace}/AGENTS.md`,
            Body: agentsContent,
            ContentType: "text/markdown",
          }), { abortSignal: activationScope.signal });
          console.log(`[contract] AGENTS.md synced to S3 (${agentsContent.length} bytes)`);
        } catch (err) {
          console.warn(`[contract] Failed to sync AGENTS.md to S3: ${err.message}`);
          configPublishError = err;
        }
      })();
      configPublishPromise = Promise.all([configPublishPromise, publish]);
    }
  }
}

/**
 * Back up the runtime-skills manifest to S3 as soon as an install/uninstall
 * rewrites it. The periodic save runs every 30 min and StopRuntimeSession /
 * idle termination do not flush, so without this a skill installed (or
 * removed) shortly before a cold start comes back in the old state — seen on
 * staging: uninstall, cold start one minute later, skill reinstalled from the
 * stale S3 manifest. Only the ~150-byte manifest is uploaded, from the
 * contract process (the skill scripts have no AWS credentials).
 */
function startRuntimeSkillManifestBackup(namespace) {
  if (runtimeSkillManifestWatcher || !namespace) return;
  const manifestFile = runtimeSkills.manifestPath(OPENCLAW_DIR);
  runtimeSkillManifestWatcher = runtimeSkills.watchManifest(
    manifestFile,
    async () => {
      const saved = await workspaceSync.saveFile(namespace, runtimeSkills.MANIFEST_NAME);
      if (saved) console.log("[contract] Runtime skill manifest changed — backed up to S3");
    },
    { log: console },
  );
  if (runtimeSkillManifestWatcher) {
    console.log(`[contract] Watching ${manifestFile} for immediate S3 backup`);
  }
}

/**
 * Generate SigV4-signed HTTP headers for a WebSocket CDP endpoint.
 * The browser automation stream requires IAM authentication — Playwright's
 * connectOverCDP sends these as HTTP upgrade request headers.
 *
 * @param {string} wsEndpoint - WebSocket URL (wss://...)
 * @returns {object} Signed headers (Authorization, X-Amz-Date, X-Amz-Security-Token, Host)
 */
async function signBrowserEndpoint(wsEndpoint) {
  const { SignatureV4 } = require("@smithy/signature-v4");
  const { Sha256 } = require("@aws-crypto/sha256-js");
  const { defaultProvider } = require("@aws-sdk/credential-provider-node");

  const url = new URL(wsEndpoint.replace(/^wss:/, "https:"));
  const region = process.env.AWS_REGION || "us-east-1";

  const signer = new SignatureV4({
    service: "bedrock-agentcore",
    region,
    credentials: defaultProvider(),
    sha256: Sha256,
  });

  const signed = await signer.sign({
    method: "GET",
    protocol: url.protocol,
    hostname: url.hostname,
    port: url.port ? Number(url.port) : undefined,
    path: url.pathname + url.search,
    headers: {
      host: url.host,
    },
  });

  // Return only the auth-relevant headers
  const result = {};
  for (const [k, v] of Object.entries(signed.headers)) {
    if (/^(authorization|x-amz-|host)$/i.test(k) || k.startsWith("x-amz-")) {
      result[k] = v;
    }
  }
  return result;
}

function browserOwner() {
  return Object.freeze({
    generation: browserGeneration, sessionId: currentBrowserSessionId, endpoint: currentBrowserEndpoint,
  });
}

function browserStartClientToken(userId, runtimeSessionId, browserIdentifier, generation) {
  if (typeof userId !== "string" || !userId.trim() ||
      typeof runtimeSessionId !== "string" || !runtimeSessionId.trim() || runtimeSessionId.length > 256) {
    throw new Error("Browser start requires a bound user and runtime session ID");
  }
  if (!Number.isSafeInteger(generation) || generation < 1) {
    throw new Error("Invalid Browser start generation");
  }
  // Generate after restore for each acquisition; SDK retries reuse the command's token.
  return createHash("sha256").update(JSON.stringify([
    "StartBrowserSession", browserIdentifier, userId, runtimeSessionId, generation,
    osRandomUUID(),
  ])).digest("hex");
}

function assertBrowserStartBinding(owner) {
  owner.scope.checkpoint();
  const current = v2Lifecycle.snapshot();
  if (shuttingDown || owner.scope !== activationScope || current.generation !== owner.scope.generation ||
      ["userId", "actorId", "runtimeSessionId", "routingGeneration"]
        .some((key) => (current.binding?.[key] ?? null) !== owner.binding[key]) ||
      currentUserId !== owner.binding.userId || currentNamespace !== owner.binding.actorId.replace(/:/g, "_") ||
      process.env.USER_ID !== owner.binding.actorId || process.env.INTERNAL_USER_ID !== owner.binding.userId ||
      process.env.BROWSER_IDENTIFIER !== owner.browserIdentifier ||
      (process.env.AWS_REGION || "us-east-1") !== owner.region) {
    throw new Error("Browser acquisition binding changed");
  }
}

function assertBrowserStartOwner(owner) {
  assertBrowserStartBinding(owner);
  if (owner !== browserStartOwner || owner.retired || owner.generation !== browserGeneration ||
      (currentBrowserSessionId && (!owner.promoted || currentBrowserSessionId !== owner.sessionId))) {
    throw new Error("Browser acquisition ownership changed");
  }
}

function retireBrowserStartOwner(owner = browserStartOwner) {
  if (!owner) return Promise.resolve();
  owner.retired = true;
  if (owner.sessionId && owner.generation === browserGeneration &&
      currentBrowserSessionId === owner.sessionId) retireBrowserOwner(browserOwner());
  if (owner.retirement) return owner.retirement;
  const retirement = Promise.resolve().then(async () => {
    await owner.acquisition.catch(() => {});
    if (owner.sessionId && browserSessions.has(owner.sessionId)) await stopBrowserResource(owner.sessionId);
  });
  owner.retirement = retirement;
  retirement.then(() => {
    owner.cleanupFailed = false;
  }, () => {
    owner.cleanupFailed = true;
    if (owner.retirement === retirement) owner.retirement = null;
  });
  return retirement;
}

function startBrowserOverlap(userId) {
  if (!process.env.BROWSER_IDENTIFIER) return null;
  if (browserStartOwner) {
    assertBrowserStartOwner(browserStartOwner);
    return browserStartOwner;
  }
  if (currentBrowserSessionId) return null;
  activationScope.checkpoint();
  const current = v2Lifecycle.snapshot();
  if (!current.binding || current.binding.userId !== userId ||
      current.generation !== activationScope.generation) throw new Error("Browser acquisition requires binding");
  const owner = {
    binding: Object.freeze(Object.fromEntries(["userId", "actorId", "runtimeSessionId", "routingGeneration"]
      .map((key) => [key, current.binding[key] ?? null]))),
    scope: activationScope, generation: browserGeneration + 1,
    browserIdentifier: process.env.BROWSER_IDENTIFIER, region: process.env.AWS_REGION || "us-east-1",
    retired: false, promoted: false, settled: false, sessionId: null,
  };
  assertBrowserStartBinding(owner);
  const clientToken = browserStartClientToken(userId, owner.binding.runtimeSessionId,
    owner.browserIdentifier, owner.generation);
  browserGeneration = owner.generation;
  browserStartOwner = owner;
  owner.acquisition = Promise.resolve().then(async () => {
    let client;
    try {
      assertBrowserStartOwner(owner);
      const { BedrockAgentCoreClient, StartBrowserSessionCommand } =
        require("@aws-sdk/client-bedrock-agentcore");
      client = new BedrockAgentCoreClient({ region: owner.region });
      const response = await client.send(new StartBrowserSessionCommand({
          browserIdentifier: owner.browserIdentifier, clientToken,
          name: userId.replace(/[^a-zA-Z0-9-]/g, "-").slice(0, 64),
          sessionTimeoutSeconds: BROWSER_SESSION_TIMEOUT_SECONDS,
          viewPort: { width: 1280, height: 720 },
        })).then((response) => {
          owner.sessionId = response.sessionId;
          if (!owner.sessionId) throw new Error("No Browser session ID returned");
          browserSessions.add(owner.sessionId);
          browserResourceOrigins.set(owner.sessionId, Object.freeze({
            browserIdentifier: owner.browserIdentifier, region: owner.region,
          }));
          owner.expiresAt = Date.now() + BROWSER_SESSION_TIMEOUT_SECONDS * 1000;
          return response;
        });
      try { assertBrowserStartOwner(owner); } catch { retireBrowserStartOwner(owner); }
      return response;
    } finally {
      owner.settled = true;
      client?.destroy?.();
    }
  });
  owner.acquisition.catch((error) => {
    if (!owner.retired && !owner.scope.signal.aborted && !shuttingDown) void v2Lifecycle.fail(error);
    retireBrowserStartOwner(owner);
  });
  owner.scope.signal.addEventListener("abort", () => { retireBrowserStartOwner(owner); }, { once: true });
  if (owner.scope.signal.aborted) retireBrowserStartOwner(owner);
  return owner;
}

function assertBrowserPromotionReady(owner) {
  assertBrowserStartOwner(owner);
  const resources = v2Lifecycle.snapshot().resources;
  if (!owner.gatewayActivationPassed || !["credentials", "restore", "skills"]
    .every((name) => resources.includes(name))) throw new Error("Browser promotion barriers are incomplete");
  workspaceSync.assertRestoreReady();
}

function initializeBrowserBranch(userId, deadline) {
  const owner = browserStartOwner;
  if (!owner) return ensureV2BrowserSession(userId, deadline);
  if (!owner.finalization) {
    owner.finalization = Promise.resolve().then(async () => {
      assertBrowserPromotionReady(owner);
      await owner.acquisition;
      assertBrowserPromotionReady(owner);
      await initBrowserSession(userId, owner);
      await ensureV2BrowserSession(userId, deadline);
      assertBrowserStartBinding(owner);
    });
    owner.finalization.catch(() => {});
  }
  return owner.finalization;
}

function ownsBrowser(owner) {
  return owner.generation === browserGeneration && owner.sessionId === currentBrowserSessionId &&
    owner.endpoint === currentBrowserEndpoint;
}

function retireBrowserOwner(owner) {
  if (!ownsBrowser(owner)) return;
  browserGeneration++;
  currentBrowserSessionId = null;
  currentBrowserEndpoint = null;
  browserExpiresAt = 0;
  clearInterval(browserHeaderRefreshTimer);
  browserHeaderRefreshTimer = null;
}

async function refreshBrowserHeaders(owner) {
  if (!ownsBrowser(owner)) return false;
  const headers = await signBrowserEndpoint(owner.endpoint);
  if (!ownsBrowser(owner)) return false;
  activationScope.checkpoint();
  fs.writeFileSync(BROWSER_SESSION_FILE, JSON.stringify({
    endpoint: owner.endpoint, sessionId: owner.sessionId, headers,
  }));
  return true;
}

function stopBrowserResource(sessionId) {
  if (browserStops.has(sessionId)) return browserStops.get(sessionId);
  const stop = (async () => {
    const { BedrockAgentCoreClient, StopBrowserSessionCommand } =
      require("@aws-sdk/client-bedrock-agentcore");
    const origin = browserResourceOrigins.get(sessionId);
    if (!origin) throw new Error("Browser cleanup origin is unavailable");
    const client = new BedrockAgentCoreClient({ region: origin.region });
    try {
      await client.send(new StopBrowserSessionCommand({
        browserIdentifier: origin.browserIdentifier, sessionId,
      }), { abortSignal: AbortSignal.timeout(10000) });
      browserSessions.delete(sessionId);
      browserResourceOrigins.delete(sessionId);
      console.log("[browser] Stopped session", sessionId);
    } finally {
      client.destroy?.();
    }
  })().finally(() => browserStops.delete(sessionId));
  browserStops.set(sessionId, stop);
  return stop;
}

async function getBrowserSessionStatus(owner, deadline) {
  activationScope.checkpoint();
  if (!owner.sessionId || !ownsBrowser(owner)) throw new Error("Browser ownership is unavailable");
  if (Date.now() >= deadline) throw new Error("Browser readiness deadline exceeded");
  const { BedrockAgentCoreClient, GetBrowserSessionCommand } =
    require("@aws-sdk/client-bedrock-agentcore");
  const client = new BedrockAgentCoreClient({ region: process.env.AWS_REGION || "us-east-1" });
  try {
    const response = await client.send(new GetBrowserSessionCommand({
      browserIdentifier: process.env.BROWSER_IDENTIFIER, sessionId: owner.sessionId,
    }), { abortSignal: AbortSignal.any([
      activationScope.signal, AbortSignal.timeout(Math.max(1, Math.min(10000, deadline - Date.now()))),
    ]) });
    activationScope.checkpoint();
    if (!ownsBrowser(owner) || response.sessionId !== owner.sessionId ||
        response.browserIdentifier !== process.env.BROWSER_IDENTIFIER) {
      throw new Error("Browser status does not match its owner");
    }
    // Use SDK status enums; prose documentation uses different names.
    if (response.status !== "READY" && response.status !== "TERMINATED") {
      throw new Error("Browser session status is unknown");
    }
    return response.status;
  } finally {
    client.destroy?.();
  }
}

async function ensureV2BrowserSession(userId, deadline) {
  if (!process.env.BROWSER_IDENTIFIER) return;
  if (!currentBrowserSessionId) await initBrowserSession(userId);
  let owner = browserOwner();
  const status = await getBrowserSessionStatus(owner, deadline);
  if (status === "TERMINATED" || browserExpiresAt <= Date.now() + TURN_TIMEOUT_MS + 60000) {
    if (status === "TERMINATED") {
      retireBrowserOwner(owner);
      browserSessions.delete(owner.sessionId);
      browserResourceOrigins.delete(owner.sessionId);
    } else {
      await stopBrowserSessions();
    }
    activationScope.checkpoint();
    await initBrowserSession(userId);
    owner = browserOwner();
    if (await getBrowserSessionStatus(owner, deadline) !== "READY") {
      throw new Error("Replacement Browser session is not ready");
    }
  }
  if (!await refreshBrowserHeaders(owner)) throw new Error("Browser ownership changed during readiness");
}

/**
 * Start an AgentCore Browser session for the given user.
 * Disabled when no Browser identifier is configured; otherwise readiness is required.
 */
async function initBrowserSession(userId, acquisition = null) {
  const browserIdentifier = process.env.BROWSER_IDENTIFIER;
  if (!browserIdentifier) return; // Feature not enabled

  // Per-user sessions use a single userId; check state vars directly
  if (currentBrowserSessionId) return; // Already initialized
  activationScope.checkpoint();
  if (acquisition) assertBrowserPromotionReady(acquisition);
  const generation = acquisition ? acquisition.generation : ++browserGeneration;
  let sessionId = null;
  let owner = null;
  let client = null;
  try {
    let response;
    if (acquisition) {
      response = await acquisition.acquisition;
      assertBrowserPromotionReady(acquisition);
    } else {
      const clientToken = browserStartClientToken(
        userId, v2Lifecycle.snapshot().binding?.runtimeSessionId, browserIdentifier, generation,
      );
      const { BedrockAgentCoreClient, StartBrowserSessionCommand } =
        require("@aws-sdk/client-bedrock-agentcore");
      client = new BedrockAgentCoreClient({ region: process.env.AWS_REGION || "us-east-1" });

      response = await client.send(new StartBrowserSessionCommand({
        browserIdentifier,
        clientToken,
        name: userId.replace(/[^a-zA-Z0-9-]/g, "-").slice(0, 64),
        sessionTimeoutSeconds: BROWSER_SESSION_TIMEOUT_SECONDS,
        viewPort: {
          width: 1280,
          height: 720,
        },
      }));
    }

    sessionId = response.sessionId;
    if (!sessionId) throw new Error("No Browser session ID returned");
    browserSessions.add(sessionId);
    if (!browserResourceOrigins.has(sessionId)) browserResourceOrigins.set(sessionId, Object.freeze({
      browserIdentifier, region: process.env.AWS_REGION || "us-east-1",
    }));
    activationScope.checkpoint();
    if (generation !== browserGeneration) throw new Error("Browser initialization was superseded");
    const endpoint = response.streams?.automationStream?.streamEndpoint;
    if (!endpoint) throw new Error("No automation stream endpoint returned");
    if (acquisition) acquisition.promoted = true;
    currentBrowserSessionId = sessionId;
    currentBrowserEndpoint = endpoint;
    browserExpiresAt = acquisition ? acquisition.expiresAt : Date.now() + BROWSER_SESSION_TIMEOUT_SECONDS * 1000;
    owner = browserOwner();
    if (!await refreshBrowserHeaders(owner)) throw new Error("Browser initialization was superseded");

    // Refresh SigV4 headers every 4 min (signatures expire after ~5 min)
    browserHeaderRefreshTimer = setInterval(async () => {
      if (!ownsBrowser(owner)) return;
      try {
        await refreshBrowserHeaders(owner);
      } catch (e) {
        if (!ownsBrowser(owner)) return;
        console.error("[browser] Failed to refresh SigV4 headers:", e.message);
        v2Lifecycle.fail(e);
      }
    }, 4 * 60 * 1000);

    console.log("[browser] Session started for", userId, "-", response.sessionId);
  } catch (err) {
    if (owner) retireBrowserOwner(owner);
    if (sessionId) {
      try { await stopBrowserResource(sessionId); } catch (stopError) {
        console.error("[browser] Failed to clean up acquired session:", stopError.message);
      }
    }
    console.error("[browser] Failed to start session for", userId, "-", err.message);
    throw err;
  } finally {
    client?.destroy?.();
  }
}

/**
 * Stop all active browser sessions. Called during SIGTERM shutdown.
 */
async function stopBrowserSessions() {
  const retirement = browserStartOwner ? retireBrowserStartOwner() : null;
  const browserIdentifier = process.env.BROWSER_IDENTIFIER;
  if (!browserIdentifier && !retirement) return;
  const sessions = [...browserSessions];
  retireBrowserOwner(browserOwner());
  try {
    await Promise.all([...(retirement ? [retirement] : []), ...sessions.map(stopBrowserResource)]);
  } catch (err) {
    console.error("[browser] Stop failed:", err.message);
    throw err;
  }
}

/**
 * Mint the per-user Cognito access token for the AgentCore Gateway MCP server.
 * The readiness check refreshes it before a turn and verifies the restarted gateway.
 */
async function setupGatewayBearer(actorId) {
  const provider = createCognitoTokenProvider({
    userPoolId: process.env.COGNITO_USER_POOL_ID || "",
    clientId: process.env.COGNITO_CLIENT_ID || "",
    passwordSecret: COGNITO_PASSWORD_SECRET || "",
    region: process.env.AWS_REGION || "us-west-2",
    log: (msg) => console.log(msg.replace(/^\[cognito\]/, "[contract] Cognito")),
  });
  if (!provider.isConfigured()) {
    throw new Error("Gateway MCP requires Cognito configuration");
  }
  gatewayBearer = await provider.getAccessToken(actorId);
  console.log(
    `[contract] Gateway MCP bearer acquired for ${actorId} (expires ${new Date(gatewayBearer.expiresAt).toISOString()})`,
  );
  gatewayTokenProvider = provider;
  activationScope.checkpoint();
}

/**
 * Initialization — called on first /invocations request.
 *
 * Bind credentials and restore state before activating the prepared child.
 */
async function init(userId, actorId, channel, channelActorId, channelTarget) {
  if (initInProgress) return initPromise;
  initInProgress = true;

  initPromise = (async () => {
    const activationStarted = Date.now();
    const logActivationPhase = (phase) => {
      console.log(
        `[contract] ${phase} elapsedMs=${Date.now() - activationStarted} generation=${activationScope.generation}`,
      );
    };
    const checkpoint = () => { activationScope.checkpoint(); };
    checkpoint();
    const namespace = actorId.replace(/:/g, "_");
    currentUserId = userId;
    currentNamespace = namespace;
    scopedLogger = cwLogger.startOwned(`${namespace}-${Date.now()}`, {
      binding: v2Lifecycle.snapshot().binding, generation: activationScope.generation,
      signal: activationScope.signal,
    });
    if (scopedLogger.captureAttached) startupTiming.enableScopedLogging();

    // Expose USER_ID so child processes (OpenClaw skill scripts) inherit it
    process.env.USER_ID = actorId;
    // Authorize EventBridge cron skill operations for this user.
    process.env.INTERNAL_USER_ID = userId;

    // Write initial identity file for the proxy to read
    updateIdentityFile(actorId, channel, channelActorId, channelTarget);
    startBrowserOverlap(userId);

    console.log(
      `[contract] Init for user=${userId} actor=${actorId} namespace=${namespace}`,
    );

    // Fetch secrets only after binding the user.
    if (!GATEWAY_TOKEN) {
      await prefetchSecrets();
      checkpoint();
    }
    if (!GATEWAY_TOKEN) {
      throw new Error(
        "Gateway token not available — cannot authenticate WebSocket connections",
      );
    }

    // 1b. Create scoped S3 credentials (per-user IAM isolation)
    // Restricts S3 access to the user's namespace prefix, preventing cross-user
    // data access even through OpenClaw's bash/code execution tools.
    if (!process.env.EXECUTION_ROLE_ARN) throw new Error("V2 requires scoped execution credentials");
    credentialManager = scopedCreds.createCredentialManager(namespace, {
      credDir: SCOPED_CREDS_DIR,
      internalUserId: userId,
      refreshBeforeMs: TURN_TIMEOUT_MS + 60000,
      onRefresh: (creds) => {
        checkpoint();
        workspaceSync.configureCredentials(creds);
      },
    });
    const ownerCredentials = credentialManager;
    workspaceSync.configureCredentialProvider(() => ownerCredentials.ensureFresh({
      forPersistence: true, minValidityMs: 60000,
    }));
    await credentialManager.ensureFresh();
    checkpoint();
    activationScope.mark("credentials");
    logActivationPhase("credentials_ready");

    // 1b'. Mint the required per-user access token when Gateway MCP is enabled.
    if (AGENTCORE_GATEWAY_URL) {
      await setupGatewayBearer(actorId);
      checkpoint();
    }

    // 1c. Session storage: local ~/.openclaw including the agent workspace
    // (the NFS mount has no SQLite locks and no hard links; see
    // state-storage.js), and the mount's mirror restored to local disk. Must
    // happen before anything touches ~/.openclaw (lock cleanup, S3 restore,
    // config/AGENTS.md write, legacy import, gateway activation).
    const liveRestore = await preparedGateway.restoreForBinding({
      lifecycle: v2Lifecycle, scope: activationScope, namespace, channel,
      token: GATEWAY_TOKEN, boundEnv: openclawEnvFor(userId), waitMs: RESTORE_WAIT_MS,
    });
    checkpoint();
    const liveAdopted = liveRestore?.status === "adopted";
    const sessionStorageAvailable = liveAdopted ? liveRestore.storage.available : setupSessionStorage();
    sessionStorageActive = sessionStorageAvailable;

    // 2. Start the Bedrock proxy with user identity env vars
    // Only pass required env vars — avoid leaking secrets via process.env spread
    const startProxy = async () => {
      checkpoint();
      console.log("[contract] Starting Bedrock proxy...");
      const proxyEnv = {
        PATH: process.env.PATH,
        HOME: process.env.HOME || "/root",
        NODE_PATH: process.env.NODE_PATH || "/app/node_modules",
        NODE_OPTIONS: process.env.NODE_OPTIONS || "",
        NODE_COMPILE_CACHE: process.env.NODE_COMPILE_CACHE,
        AWS_REGION: process.env.AWS_REGION || "us-west-2",
        BEDROCK_MODEL_ID: process.env.BEDROCK_MODEL_ID || "",
        BEDROCK_FALLBACK_MODEL_ID: process.env.BEDROCK_FALLBACK_MODEL_ID || "",
        COGNITO_USER_POOL_ID: process.env.COGNITO_USER_POOL_ID || "",
        COGNITO_CLIENT_ID: process.env.COGNITO_CLIENT_ID || "",
        COGNITO_PASSWORD_SECRET: COGNITO_PASSWORD_SECRET || "",
        S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET || "",
        SUBAGENT_MODEL_NAME: SUBAGENT_MODEL_NAME,
        SUBAGENT_BEDROCK_MODEL_ID: process.env.SUBAGENT_BEDROCK_MODEL_ID || "",
        // Bedrock Guardrails: agentcore-proxy.js reads these at load time; without them the
        // guardrailConfig is never attached to Converse calls even when AgentCore Runtime has them.
        BEDROCK_GUARDRAIL_ID: process.env.BEDROCK_GUARDRAIL_ID || "",
        BEDROCK_GUARDRAIL_VERSION: process.env.BEDROCK_GUARDRAIL_VERSION || "",
        // Optional temperature override read by agentcore-proxy.js ("none" omits it; unset = per-model default).
        BEDROCK_TEMPERATURE: process.env.BEDROCK_TEMPERATURE || "",
        // Allowlisted per-user /model overrides (comma-separated; empty = feature disabled).
        BEDROCK_USER_SELECTABLE_MODEL_IDS: process.env.BEDROCK_USER_SELECTABLE_MODEL_IDS || "",
        USER_ID: actorId,
        INTERNAL_USER_ID: userId,  // container internal userId for skill authorization
        CHANNEL: channel,
        OPENCLAW_SKIP_CRON: "1", // Disable internal cron — EventBridge handles scheduling
      };
      const scope = activationScope;
      const binding = Object.freeze({ ...v2Lifecycle.snapshot().binding });
      const credentials = credentialManager;
      const child = proxySdkOwner.child;
      const assertCurrent = () => {
        checkpoint();
        const current = v2Lifecycle.snapshot();
        if (shuttingDown || child !== proxyProcess || child !== proxySdkOwner.child ||
            scope !== activationScope || current.generation !== scope.generation ||
            ["userId", "actorId", "runtimeSessionId", "routingGeneration"].some(
              (key) => (current.binding?.[key] ?? null) !== (binding[key] ?? null)) ||
            currentUserId !== userId || currentNamespace !== namespace ||
            process.env.USER_ID !== actorId || process.env.INTERNAL_USER_ID !== userId ||
            !secretsReady || !GATEWAY_TOKEN || credentialManager !== credentials ||
            (COGNITO_PASSWORD_SECRET || "") !== proxyEnv.COGNITO_PASSWORD_SECRET ||
            !current.resources.includes("restore") || !current.resources.includes("credentials") ||
            (AGENTCORE_GATEWAY_URL && !gatewayBearer?.token) ||
            ["BEDROCK_MODEL_ID", "BEDROCK_FALLBACK_MODEL_ID", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID",
              "S3_USER_FILES_BUCKET", "SUBAGENT_BEDROCK_MODEL_ID", "BEDROCK_GUARDRAIL_ID",
              "BEDROCK_GUARDRAIL_VERSION", "BEDROCK_TEMPERATURE", "BEDROCK_USER_SELECTABLE_MODEL_IDS"].some(
              (key) => (process.env[key] || "") !== proxyEnv[key]) ||
            JSON.parse(fs.readFileSync(IDENTITY_FILE, "utf8")).actorId !== actorId) {
          throw new Error("Prepared Proxy binding is unavailable");
        }
        workspaceSync.assertRestoreReady();
      };
      await proxySdkOwner.activate({ env: proxyEnv, binding, generation: scope.generation,
        signal: scope.signal, assertCurrent });
      assertCurrent();
      proxyAuthControl = createProxyAuthParent({
        child, generation: scope.generation, signal: scope.signal,
        isCurrent: () => { try { assertCurrent(); return true; } catch { return false; } },
      });
    };

    // 1e. Restore workspace from S3 and configure sync mode based on session
    // storage availability. This is awaited (bounded) BEFORE gateway activation:
    // OpenClaw 2.0 opens its per-agent SQLite store and checks for a legacy
    // sessions.json at startup, so restoring files underneath a booting gateway
    // (the pre-2.0 behaviour) could hand it a half-restored state dir.
    //
    // The S3 restore ALWAYS runs: workspace-sync uploads nothing until it has
    // completed (or S3 confirmed there is no saved state), and nothing at all
    // from a container whose restore failed.
    if (liveAdopted) {
      workspaceSync.setBackupMode(true);
      checkpoint();
      workspaceSync.assertRestoreReady();
    } else {
      let restorePromise;
      if (sessionStorageAvailable) {
        // Session storage is primary — S3 becomes a cold backup (30min instead of 5min)
        workspaceSync.setBackupMode(true);

        // Check if session storage already has data (returning user). Empty
        // directories created by setupSessionStorage() itself do not count.
        const hasContent = stateStorage.sessionStorageHasContent(MOUNTED_OPENCLAW_DIR);

        if (hasContent) {
          // The mount's mirror may be partial: on staging a mid-turn restart found
          // only the workspace files there (the 2 s workspace mirror had run, the
          // 5 min state mirror had not), started with no state DBs or skill
          // manifest, and the change backup then uploaded a fresh empty database
          // over the good S3 copy. Fill in whatever is missing from S3 without
          // touching the (fresher) files the mount did have.
          console.log("[contract] Session storage has existing data — restoring only missing files from S3");
          restorePromise = workspaceSync.restoreWorkspace(namespace, { overwrite: false });
        } else {
          console.log("[contract] Session storage is empty — restoring from S3 backup");
          restorePromise = workspaceSync.restoreWorkspace(namespace);
        }
      } else {
        // No session storage — use S3 sync as primary (existing behavior)
        restorePromise = workspaceSync.restoreWorkspace(namespace);
      }
      // Reject incomplete restoration before any user state is written.
      await workspaceSync.awaitRestore(restorePromise, RESTORE_WAIT_MS);
      checkpoint();
      workspaceSync.assertRestoreReady();
    }
    activationScope.mark("restore");
    logActivationPhase("restore_ready");
    if (!liveAdopted) await cleanupLockFiles();
    checkpoint();
    await startProxy();

    // 1e'. The restore brings back every pre-2.0 file `openclaw doctor --fix`
    // retired on the upgrade boot (S3 never sees deletes — that is the rollback
    // path), and the 2.0 gateway refuses to run while they exist. Remove the
    // ones whose bytes still match doctor's input; a changed one needs doctor
    // again (F5, us-west-2 F1 staging test).
    const resurrected = liveAdopted ? { changed: [] } : pruneResurrectedLegacyFiles();

    // 1f. Write OpenClaw config + AGENTS.md AFTER the session-storage restore
    // (so they are not overwritten by stale restored data) and
    // BEFORE gateway activation (2.0 validates config strictly at startup; the
    // pre-2.0 code relied on gateway boot latency to win this race).
    writeOpenClawConfig({ preserveSeedFiles: liveAdopted });

    // 1g. OpenClaw 2.0 moved session state to per-agent SQLite. A pre-2.0
    // sessions.json in the state dir makes the gateway REFUSE READINESS until
    // `openclaw doctor --fix` imports it (docs/concepts/session.md, "Where state
    // lives"). Run the import here, before activation, so upgraded users keep their
    // history and the gateway boots. No-op when no legacy store is present.
    // Doctor needs the config above to exist; it may also rewrite it (dropping
    // keys, rotating a .bak), so re-emit our config afterwards.
    const openclawEnv = openclawEnvFor(userId);
    const migrated = liveAdopted ? false : await migrateLegacySessionStore(openclawEnv, { forceDoctor: resurrected.changed.length > 0 });
    checkpoint();
    if (migrated) {
      writeOpenClawConfig();
    }
    await configPublishPromise;
    checkpoint();
    if (configPublishError) throw configPublishError;
    const skills = await runtimeSkills.reinstallFromManifest({
      manifestFile: runtimeSkills.manifestPath(OPENCLAW_DIR), log: console, run: runRuntimeSkillInstall,
    });
    checkpoint();
    if (skills.failed || skills.corrupt || skills.invalid) throw new Error("Required runtime skills could not be restored");
    activationScope.mark("skills");
    logActivationPhase("config_skills_ready");

    // 1h. Mirror workspace changes onto the mount within seconds of the
    // gateway (or a skill) writing them; the periodic/shutdown mirrors cover
    // the SQLite state. Started before activation so the 2.0 bootstrap seeding
    // (AGENTS.md, SOUL.md, ...) is captured too.
    if (sessionStorageActive) {
      stateStorage.startWorkspaceWatcher({
        stateDir: OPENCLAW_DIR,
        mountStateDir: MOUNTED_OPENCLAW_DIR,
        log: console,
        assertReady: workspaceSync.assertRestoreReady,
      });
      if (liveAdopted) {
        const mirrored = stateStorage.flushWorkspaceMirror("live-seed-initial");
        if (!mirrored || mirrored.skipped) throw new Error("Live seed initial mirror failed");
        checkpoint();
      }
    }

    console.log("[contract] Starting OpenClaw gateway (headless)...");
    // openclawEnv: scoped env built above — excludes container credentials,
    // uses credential_process for scoped S3 access only (see openclawEnvFor).
    checkpoint();
    lastOpenClawEnv = openclawEnv;
    await preparedGateway.activate({
      env: openclawEnv, binding: v2Lifecycle.snapshot().binding,
      generation: activationScope.generation, signal: activationScope.signal,
      deadline: Date.now() + ACTIVATION_TIMEOUT_MS,
    });
    checkpoint();

    if (browserStartOwner) {
      assertBrowserStartOwner(browserStartOwner);
      browserStartOwner.gatewayActivationPassed = true;
    }
    const gatewayBranch = async () => {
      // 2. Wait only for proxy readiness (~5s)
      proxyReady = await waitForPort(PROXY_PORT, "Proxy", 30000, 1000);
      checkpoint();
      if (!proxyReady) {
        throw new Error("Proxy failed to start within 30s");
      }
      await assertProxyOutcomeKnown();
      checkpoint();
      activationScope.mark("proxy");
      await waitForGateway({
        port: OPENCLAW_PORT, token: GATEWAY_TOKEN, signal: activationScope.signal,
        deadline: Date.now() + ACTIVATION_TIMEOUT_MS, configProbe: true,
      });
      checkpoint();
      // A suspended sub-agent delivery stays in every turn's context for 7 days
      // and gets narrated into replies. Bounded and non-throwing.
      await dismissStaleSuspendedDeliveries({
        url: `ws://127.0.0.1:${OPENCLAW_PORT}`, token: GATEWAY_TOKEN, signal: activationScope.signal,
        deadline: Date.now() + 15000,
      });
      checkpoint();
      openclawReady = true;
      activationScope.mark("gateway");
      logActivationPhase("gateway_ready");
    };
    const browserBranch = () => initializeBrowserBranch(userId, Date.now() + ACTIVATION_TIMEOUT_MS);
    let branchError;
    const observe = (work) => Promise.resolve().then(work).catch((error) => {
      branchError ||= error;
      void v2Lifecycle.fail(error);
      throw error;
    });
    await Promise.allSettled([observe(gatewayBranch), observe(browserBranch)]);
    if (branchError) throw branchError;
    checkpoint();
    activationScope.mark("browser");
    logActivationPhase("browser_ready");
    workspaceSync.startPeriodicSave(namespace);
    if (workspaceSync.startChangeBackup(namespace, liveAdopted ? { initialSweep: true } : undefined).mode === "none") {
      throw new Error("Required state backup is unavailable");
    }
    startRuntimeSkillManifestBackup(namespace);
    if (!runtimeSkillManifestWatcher) throw new Error("Required skill manifest watcher is unavailable");
    if (sessionStorageActive) {
      stateStorage.startPeriodicMirror({
        stateDir: OPENCLAW_DIR, mountStateDir: MOUNTED_OPENCLAW_DIR, log: console,
        assertReady: workspaceSync.assertRestoreReady,
      });
    }
    await credentialManager.ensureFresh();
    checkpoint();
    activationScope.mark("persistence");
    await scopedLogger.ready;
    checkpoint();
    logActivationPhase("user_ready");
    console.log("[contract] USER_READY platform=V2");
  })();

  try {
    await initPromise;
  } catch (err) {
    void v2Lifecycle.fail(err);
    try { await retireBrowserStartOwner(); } catch {}
    // Reset initPromise on failure so concurrent requests don't await a stale rejected promise
    initPromise = null;
    throw err;
  } finally {
    initInProgress = false;
  }
}

/**
 * Extract plain text from message content — handles string, array of content
 * blocks, JSON-serialized array of content blocks, or object with text/content.
 *
 * Recursively unwraps nested content blocks (common with subagent responses
 * where each layer wraps the previous one in content block JSON).
 */
function extractTextFromContent(content) {
  if (!content) return "";
  // Already a parsed array of content blocks
  if (Array.isArray(content)) {
    const text = content
      .filter((b) => b.type === "text")
      .map((b) => b.text)
      .join("");
    // Recurse in case the inner text is itself a JSON content block array
    return extractTextFromContent(text);
  }
  if (typeof content === "string") {
    // Check if the string is a JSON-serialized array of content blocks
    const trimmed = content.trim();
    if (trimmed.startsWith("[{") && trimmed.endsWith("]")) {
      let parsed = null;
      try {
        parsed = JSON.parse(trimmed);
      } catch {
        // Retry with literal control characters escaped (JS JSON.parse is strict)
        try {
          const sanitized = trimmed.replace(/[\x00-\x1f\x7f]/g, c => {
            const e = {"\b":"\\b","\t":"\\t","\n":"\\n","\f":"\\f","\r":"\\r"};
            return e[c] || ("\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
          });
          parsed = JSON.parse(sanitized);
        } catch {
          // Both failed — try regex extraction below
        }
      }
      if (!parsed) {
        // Regex fallback for malformed JSON (e.g., "text","value" instead of "text":"value")
        const textMatch = trimmed.match(/[,{]\s*"text"\s*[,:]\s*"((?:[^"\\]|\\.)*)"/);
        if (textMatch) {
          try {
            const extracted = JSON.parse('"' + textMatch[1] + '"');
            if (extracted) return extractTextFromContent(extracted);
          } catch {
            return extractTextFromContent(textMatch[1]);
          }
        }
      }
      if (
        parsed &&
        Array.isArray(parsed) &&
        parsed.length > 0 &&
        parsed.every((b) => typeof b === "object" && b !== null) &&
        parsed.some((b) => typeof b.type === "string")
      ) {
        const text = parsed
          .filter((b) => b.type === "text")
          .map((b) => b.text)
          .join("");
        // Preserve leading whitespace from original string, recurse to unwrap further nesting
        const leading = content.match(/^(\s*)/)[0];
        return extractTextFromContent(leading + text);
      }
    }
    // Detect truncated content block JSON (e.g., "\n\n[{" or "\n\n[{"type":"text"...")
    // These are partial content blocks from streaming that shouldn't leak as response text
    if (trimmed.startsWith("[{") && !trimmed.endsWith("]")) {
      if (/^\[\{\s*"type"\s*:/.test(trimmed) || trimmed === "[{") {
        return "";
      }
    }
    // Plain text string
    return content;
  }
  // Object with text or content property (e.g., {role: "assistant", content: "..."})
  if (typeof content === "object" && content !== null) {
    if (typeof content.text === "string")
      return extractTextFromContent(content.text);
    if (typeof content.content === "string")
      return extractTextFromContent(content.content);
    if (Array.isArray(content.content)) {
      const text = content.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("");
      return extractTextFromContent(text);
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// Telegram progressive streaming helpers
// ---------------------------------------------------------------------------

const https = require("https");

/**
 * Call the Telegram Bot API. Returns parsed JSON response.
 */
function telegramApiCall(method, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body);
    const req = https.request(
      {
        hostname: "api.telegram.org",
        path: `/bot${TELEGRAM_BOT_TOKEN}/${method}`,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: 10000,
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve({ ok: false, description: data });
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Telegram API timeout"));
    });
    req.end(payload);
  });
}

/**
 * Create a Telegram streamer that shows a "typing..." indicator while working.
 *
 * onDelta(text): starts a typing indicator loop (sendChatAction every 5s).
 * finalize(text, {callerGone}): stops the typing loop. Normally it does NOT
 * send the reply: the router converts markdown to Telegram HTML (parse_mode
 * HTML) and splits to the 4096 UTF-16 limit. A plain {chat_id, text} send here
 * succeeded for every short reply, set `streamed`, made the router skip its
 * send, and left users with raw markdown. messageId is null so `streamed`
 * stays false.
 *
 * Exception: when the HTTP caller (the router) has already gone away, e.g. its
 * invoke read timeout expired on a long turn, nobody will send the reply, so
 * finalize sends it itself as plain text split to the UTF-16 limit.
 */
function createTelegramStreamer(chatId) {
  let typingInterval = null;
  let typingStarted = false;

  const sendTyping = async () => {
    try {
      await telegramApiCall("sendChatAction", {
        chat_id: chatId,
        action: "typing",
      });
    } catch (err) {
      console.warn(`[telegram-stream] Typing indicator error: ${err.message}`);
    }
  };

  const startTypingLoop = () => {
    if (typingStarted) return;
    typingStarted = true;
    sendTyping();
    typingInterval = setInterval(sendTyping, 5000);
    console.log(`[telegram-stream] Typing indicator started for chat_id=${chatId}`);
  };

  const stopTypingLoop = () => {
    if (typingInterval) {
      clearInterval(typingInterval);
      typingInterval = null;
    }
  };

  const onDelta = (text) => {
    if (!text || text.length < 60) return;
    startTypingLoop();
  };

  const sendOrphanedReply = async (text) => {
    const chunks = splitTelegramText(text);
    let lastId = null;
    let sent = 0;
    for (const chunk of chunks) {
      try {
        const resp = await telegramApiCall("sendMessage", {
          chat_id: chatId,
          text: chunk,
        });
        if (resp && resp.ok) {
          sent++;
          lastId = (resp.result && resp.result.message_id) || lastId;
        } else {
          console.warn(
            "[telegram-stream] Orphaned reply chunk rejected: %s",
            (resp && resp.description) || "unknown error",
          );
        }
      } catch (err) {
        console.warn("[telegram-stream] Orphaned reply chunk error: %s", err.message);
      }
    }
    console.warn(
      "[telegram-stream] Caller disconnected before reply; contract sent %s of %s chunk(s) for chat_id=%s",
      sent,
      chunks.length,
      chatId,
    );
    return lastId;
  };

  const finalize = async (text, opts = {}) => {
    stopTypingLoop();
    if (opts.callerGone && text && text.trim()) {
      return { messageId: await sendOrphanedReply(text) };
    }
    return { messageId: null };
  };

  return { onDelta, finalize };
}

// Telegram's sendMessage limit is 4096 characters counted in UTF-16 code
// units. JS string length is already in UTF-16 units. Python equivalent:
// _split_telegram_text in lambda/router/index.py.
const TELEGRAM_MAX_UTF16_UNITS = 4096;

/**
 * Split text into chunks of at most `limit` UTF-16 code units, preferring a
 * paragraph, line or word break in the second half of each window. Never cuts
 * inside a surrogate pair; the chunks concatenate back to the original text.
 */
function splitTelegramText(text, limit = TELEGRAM_MAX_UTF16_UNITS) {
  const chunks = [];
  let rest = text || "";
  while (rest) {
    if (rest.length <= limit) {
      chunks.push(rest);
      break;
    }
    let fit = limit;
    const code = rest.charCodeAt(fit - 1);
    if (code >= 0xd800 && code <= 0xdbff) fit--; // keep the surrogate pair whole
    const window = rest.slice(0, fit);
    let cut = fit;
    for (const sep of ["\n\n", "\n", " "]) {
      const pos = window.lastIndexOf(sep);
      if (pos >= 0 && pos + sep.length > fit / 2) {
        cut = pos + sep.length;
        break;
      }
    }
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  return chunks;
}

/**
 * Watch an HTTP response for its caller going away before the response is
 * written (client closed or reset the connection). Returns a function that
 * reports whether that has happened.
 */
function trackCallerDisconnect(res) {
  let gone = false;
  res.on("close", () => {
    if (!res.writableFinished) gone = true;
  });
  return () =>
    gone ||
    (!res.writableFinished &&
      (res.destroyed || Boolean(res.socket && res.socket.destroyed)));
}

/**
 * Build bridge text from message payload.
 * Handles structured messages with images and plain text.
 */
function buildBridgeText(message) {
  if (
    typeof message === "object" &&
    message !== null &&
    Array.isArray(message.images)
  ) {
    return (
      (message.text || "") +
      "\n\n[OPENCLAW_IMAGES:" +
      JSON.stringify(message.images) +
      "]"
    );
  }
  if (typeof message === "string") {
    return message;
  }
  return String(message);
}

async function handleV2Invocation(payload, req, res, callerGone, arrival) {
  const { action, userId, actorId, message } = payload;
  const reply = (body, status = 200) => {
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };
  const runtimeSessionHeader = req.headers["x-amzn-bedrock-agentcore-runtime-session-id"];
  if (!userId || !actorId || (action !== "warmup" && !message)) {
    return reply({ error: "Missing userId, actorId, or message" }, 400);
  }
  if (payload.stream !== undefined && typeof payload.stream !== "boolean") {
    return reply({ error: "stream must be a boolean" }, 400);
  }
  const callerBudgetMs = payload.callerBudgetMs;
  if (callerBudgetMs !== undefined && (!Number.isSafeInteger(callerBudgetMs) ||
      callerBudgetMs <= 0 || callerBudgetMs > MAX_CALLER_BUDGET_MS)) {
    return reply({ error: "callerBudgetMs must be a positive integer of at most 900000" }, 400);
  }
  const callerDeadline = callerBudgetMs === undefined ? null
    : Date.now() + callerBudgetMs - CALLER_REPLY_MARGIN_MS;
  if ((runtimeSessionHeader && payload.runtimeSessionId && runtimeSessionHeader !== payload.runtimeSessionId) ||
      (payload.routingGeneration !== undefined &&
        (!Number.isSafeInteger(payload.routingGeneration) || payload.routingGeneration < 0))) {
    return reply({ error: "Invalid AgentCore Runtime session binding" }, 400);
  }
  const channel = payload.channel || "unknown";
  const channelActorId = payload.channelActorId ?? null;
  if (channelActorId !== null &&
      (typeof channelActorId !== "string" || !channelActorId.startsWith(`${channel}:`) ||
        !channelActorId.slice(channel.length + 1))) {
    return reply({ error: "Invalid channel delivery identity" }, 400);
  }
  const channelTarget = payload.channelTarget ?? null;
  if (channelTarget !== null && (typeof channelTarget !== "string" || !channelTarget)) {
    return reply({ error: "Invalid channel delivery target" }, 400);
  }
  const modelOverride = payloadModelOverride(payload.modelOverride);
  const context = Object.freeze({
    userId, actorId, channel, channelActorId, channelTarget, modelOverride,
    runtimeSessionId: runtimeSessionHeader || payload.runtimeSessionId || null,
    routingGeneration: payload.routingGeneration ?? null,
    // Bounds the queue wait; the turn itself is re-bounded at dispatch below.
    deadline: callerDeadline ?? Date.now() + TURN_TIMEOUT_MS,
  });
  try {
    v2Lifecycle.assertBinding(context);
    if (proxyModelOutcomeUnknown) {
      return reply({
        response: UNKNOWN_OUTCOME_RESPONSE, status: "unknown_outcome",
        userId, sessionId: payload.sessionId || null,
        execution: { engine: "openclaw", phase: "unknown_outcome", run_id: null },
      });
    }
    v2Lifecycle.checkpoint();
  } catch {
    return reply({ response: "This AgentCore Runtime cannot accept the requested user binding.", status: "error" });
  }
  lastActivityTime = Math.floor(Date.now() / 1000);
  activeTaskCount++;
  if (action === "warmup" && v2Lifecycle.snapshot().state !== "USER_READY") {
    v2Lifecycle.ensureReady(context).catch((error) => {
      console.error(`[contract] V2 activation failed: ${error.message}`);
    }).finally(() => { activeTaskCount = Math.max(0, activeTaskCount - 1); });
    return reply({ status: "initializing" });
  }
  if (action === "warmup" && activeTaskCount > 1) {
    // Another turn is running on this ready binding. Queueing the warmup behind
    // it would block a second brief's warmup for the whole first brief; the
    // queued turn re-verifies resources and proxy health before it dispatches.
    activeTaskCount = Math.max(0, activeTaskCount - 1);
    return reply({ status: "ready" });
  }
  const stream = action === "chat" && payload.stream === true
    ? require("./invocation-stream").createInvocationStream(req, res, arrival, TURN_TIMEOUT_MS) : null;
  try {
    const result = await v2Lifecycle.run(context, async (queued) => {
      // With a caller budget, time spent queued behind another turn is not charged to this one.
      const turn = callerDeadline === null ? queued
        : Object.freeze({ ...queued, deadline: Math.min(Date.now() + TURN_TIMEOUT_MS, callerDeadline) });
      await v2Lifecycle.recover(() => verifyV2Resources(turn), turn.deadline - Date.now());
      const before = await checkProxyHealth().catch(() => null);
      const dispatchAllowed = before?.model_outcome_unknown === false;
      if (!dispatchAllowed) proxyModelOutcomeUnknown = true;
      if (action === "warmup" && dispatchAllowed) return { status: "ready" };
      stream?.signal.throwIfAborted();
      stream?.ready(v2Lifecycle.snapshot());
      updateIdentityFile(turn.actorId, turn.channel, turn.channelActorId, turn.channelTarget,
        turn.modelOverride, { trigger: action, sessionId: turn.runtimeSessionId });
      let streamer = null;
      if (action === "chat" && TELEGRAM_BOT_TOKEN && turn.channel === "telegram" &&
          turn.channelActorId && turn.channelTarget) {
        streamer = createTelegramStreamer(turn.channelTarget);
      }
      let outcome = { phase: "unknown_outcome", response: "", runId: null };
      if (dispatchAllowed) {
        try {
          outcome = await gatewayTurn({
            url: `ws://127.0.0.1:${OPENCLAW_PORT}`, token: GATEWAY_TOKEN,
            message: buildBridgeText(message), deadline: turn.deadline,
            signal: stream ? AbortSignal.any([activationScope.signal, stream.signal]) : activationScope.signal,
            onDelta: (text) => { streamer?.onDelta(text); stream?.delta(text); },
            extractText: extractTextFromContent,
          });
        } catch {
          // A thrown transport error cannot prove that no turn ran.
        }
        const after = await checkProxyHealth().catch(() => null);
        if (after?.model_outcome_unknown !== false) {
          outcome = { ...outcome, phase: "unknown_outcome" };
        }
      }
      if (outcome.phase === "unknown_outcome") proxyModelOutcomeUnknown = true;
      const response = outcome.phase === "completed"
        ? outcome.response || "The request completed without a text response."
        : outcome.phase === "unknown_outcome"
          ? UNKNOWN_OUTCOME_RESPONSE
          : "The agent could not complete this request.";
      let streamed = false;
      if (streamer) {
        try {
          const delivery = await streamer.finalize(response, { callerGone: callerGone() });
          streamed = !!delivery.messageId;
        } catch (error) {
          console.warn(`[contract] Telegram finalization failed: ${error.message}`);
        }
      }
      if (outcome.phase === "unknown_outcome") {
        await v2Lifecycle.fail(new Error("Gateway turn outcome is unknown"));
      }
      const lifecycle = v2Lifecycle.snapshot();
      const binding = lifecycle.binding;
      return {
        response, userId: binding.userId, sessionId: payload.sessionId || null,
        runtimePlatform: RUNTIME_PLATFORM, phase: lifecycle.state,
        execution: { engine: turn.engine, phase: outcome.phase, run_id: outcome.runId },
        actorId: binding.actorId, runtimeSessionId: binding.runtimeSessionId,
        channelActorId: turn.channelActorId, channel: turn.channel, channelTarget: turn.channelTarget,
        routingGeneration: binding.routingGeneration,
        activationGeneration: lifecycle.generation,
        ...(streamed ? { streamed: true } : {}),
        ...(outcome.phase !== "completed" ? {
          status: outcome.phase === "unknown_outcome" ? "unknown_outcome" : "error",
        } : {}),
      };
    });
    if (stream) {
      if (result.execution?.phase === "completed") stream.done(result);
      else stream.error(result.status === "unknown_outcome" ? "UNKNOWN_OUTCOME" : "CHAT_FAILED");
    } else reply(result);
  } catch (error) {
    console.error(`[contract] V2 invocation failed: ${error.message}`);
    if (proxyModelOutcomeUnknown) await v2Lifecycle.fail(new Error("Model turn outcome is unknown"));
    if (stream) stream.error(proxyModelOutcomeUnknown ? "UNKNOWN_OUTCOME" : "INVOCATION_FAILED");
    else if (proxyModelOutcomeUnknown) reply({
      response: UNKNOWN_OUTCOME_RESPONSE, status: "unknown_outcome",
      userId, sessionId: payload.sessionId || null,
      execution: { engine: "openclaw", phase: "unknown_outcome", run_id: null },
    });
    else reply({
      response: "Agent initialization or readiness failed. No turn was dispatched.",
      userId, sessionId: payload.sessionId || null, status: "error",
    });
  } finally {
    stream?.close();
    activeTaskCount = Math.max(0, activeTaskCount - 1);
  }
}

// Shape check for the Router's per-user `/model` override. The proxy accepts it
// only when it is also in BEDROCK_USER_SELECTABLE_MODEL_IDS.
function payloadModelOverride(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === "string" && /^[a-z0-9][a-z0-9.-]{0,127}(?::[0-9]{1,3})?$/.test(value)) {
    return value;
  }
  console.warn("[contract] Ignoring malformed modelOverride in invocation payload");
  return null;
}

/**
 * AgentCore contract HTTP server.
 */
const server = http.createServer(async (req, res) => {
  const handlerArrival = startupTiming.captureHttpHandler(req.method, req.url);
  // GET /ping — AgentCore health check
  if (req.method === "GET" && req.url === "/ping") {
    pingCount++;
    const now = Date.now();
    const uptimeSec = Math.floor((now - startTime) / 1000);
    // HealthyBusy prevents AgentCore from terminating during active tasks.
    // Healthy allows natural idle termination when no tasks are running.
    const health = v2Lifecycle.health(activeTaskCount);
    const status = health.body.status;
    const responseBody = {
      status,
      time_of_last_update: lastActivityTime,
      active_tasks: activeTaskCount,
    };

    // Log every ping for the first 5 minutes, then every 60s
    if (uptimeSec < 300 || now - lastPingLogTime >= PING_LOG_INTERVAL_MS) {
      console.log(
        `[contract] /ping #${pingCount} uptime=${uptimeSec}s status=${responseBody.status} openclawReady=${openclawReady} proxyReady=${proxyReady} activeTasks=${activeTaskCount}`,
      );
      lastPingLogTime = now;
    }

    res.writeHead(health.statusCode, { "Content-Type": "application/json" });
    res.end(JSON.stringify(responseBody));
    return;
  }


  // POST /invocations — Chat handler
  if (req.method === "POST" && req.url === "/invocations") {
    // If the router's invoke read timeout expires on a long turn, it drops the
    // connection and will never send the Telegram reply; finalize() needs to know.
    const callerGone = trackCallerDisconnect(res);
    // Decode once from Buffers so a UTF-8 character split across chunks survives.
    readBody(req, MAX_BODY_SIZE).then(async (body) => {
      try {
        const payload = body ? JSON.parse(body) : {};
        startupTiming.recordHttpHandler(handlerArrival, payload?.startupAttemptId);
        const action = payload.action || "status";

        // Status check (no init needed)
        if (action === "status") {
          // Fetch proxy /health for request counters (non-blocking — null on failure)
          const proxyHealth = proxyReady ? await checkProxyHealth() : null;

          const diag = {
            buildVersion: BUILD_VERSION,
            uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
            currentUserId,
            openclawReady,
            proxyReady,
            secretsReady,
            openclawExitCode,
            openclawPid: openclawProcess?.pid || null,
            openclawLogs: openclawLogs.slice(-20),
            totalRequestCount: proxyHealth?.total_requests ?? null,
            subagentRequestCount: proxyHealth?.subagent_requests ?? null,
            activeTaskCount,
            pingStatus: v2Lifecycle.health(activeTaskCount).body.status,
            runtimePlatform: RUNTIME_PLATFORM,
            lifecycle: v2Lifecycle.snapshot(),
          };
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ response: JSON.stringify(diag) }));
          return;
        }
        if (["warmup", "chat", "cron"].includes(action)) {
          await handleV2Invocation({ ...payload, action }, req, res, callerGone, handlerArrival);
          return;
        }

        // Unknown action
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({ response: "Unknown action", status: "running" }),
        );
      } catch (err) {
        console.error("[contract] Invocation error:", err.message, err.stack);
        // Return 200 with generic error — AgentCore treats 500 as infrastructure failure.
        // Never expose stack traces or internal details to callers.
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            response: "An internal error occurred. Please try again.",
          }),
        );
      }
    }, (err) => {
      if (err.code === "BODY_TOO_LARGE") {
        res.writeHead(413, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Request body too large" }));
      }
      req.destroy();
    });
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

// --- SIGTERM handler: save workspace and exit gracefully ---
process.on("SIGTERM", async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  const sdkStop = proxySdkOwner?.stop();
  sdkStop?.catch(() => {});
  retireBrowserStartOwner().catch(() => {});
  scopedLogger?.retire();
  proxyAuthControl?.stop();
  proxyAuthControl = null;
  credentialManager?.beginDrain();
  v2Lifecycle.drain();
  console.log(
    "[contract] SIGTERM received — saving workspace and shutting down",
  );

  if (browserHeaderRefreshTimer) {
    clearInterval(browserHeaderRefreshTimer);
    browserHeaderRefreshTimer = null;
  }
  if (runtimeSkillManifestWatcher) {
    runtimeSkillManifestWatcher.close();
    runtimeSkillManifestWatcher = null;
  }

  // Save state to session storage + workspace to S3 (bounded)
  const saveTimeout = setTimeout(() => {
    credentialManager?.stop();
    console.warn("[contract] Workspace save timeout — exiting");
    process.exit(0);
  }, 10000 + GATEWAY_STOP_WAIT_MS);

  // 0. Upload what the change backup already knows is dirty — now, in parallel
  //    with the gateway stop. On AgentCore the container is gone well under 5 s
  //    after SIGTERM (staging logs), so this is the step most likely to finish;
  //    everything below is best effort.
  const urgentSave = workspaceSync.flushPendingSaves("sigterm", { force: true });

  // 1. Stop the gateway so its SQLite databases are closed and checkpointed.
  //    Its own shutdown writes are seen by the change watcher (still running),
  //    so upload those last changes next, then ship the log lines so far.
  stateStorage.stopPeriodicMirror();
  const exited = await stopGateway(GATEWAY_STOP_WAIT_MS);
  void preparedGateway?.stop("shutdown").catch(() => {});
  console.log(
    `[contract] Gateway ${exited ? "stopped" : "still running"} before shutdown snapshot`,
  );
  await urgentSave;
  await workspaceSync.stopChangeBackup("sigterm");
  cwLogger.flush().catch(() => {});

  // 2. Snapshot the state dir onto the mount — what the next cold start
  //    restores before activation, when the mount survives the stop.
  stateStorage.stopWorkspaceWatcher();
  if (sessionStorageActive) {
    await mirrorStateToSessionStorage("shutdown");
  }

  // 3. Full S3 backup (workspace files + state dir snapshots, all from local
  //    disk). Files uploaded above are skipped by content hash.
  try {
    await workspaceSync.cleanup(currentNamespace);
  } catch (err) {
    console.warn(`[contract] Workspace cleanup error: ${err.message}`);
  } finally {
    credentialManager?.stop();
  }

  // Stop browser sessions before exit
  try {
    await stopBrowserSessions();
  } catch (err) {
    console.warn(`[contract] Browser session cleanup error: ${err.message}`);
  }

  if (sdkStop) { try { await sdkStop; } catch (error) {
    console.warn(`[contract] Proxy cleanup error: ${error.message}`);
  } }
  clearTimeout(saveTimeout);

  // Kill child processes
  if (openclawProcess) {
    try {
      openclawProcess.kill("SIGTERM");
    } catch {}
  }
  if (proxyProcess) {
    try {
      proxyProcess.kill("SIGTERM");
    } catch {}
  }

  await cwLogger.shutdown();
  console.log("[contract] Shutdown complete");
  process.exit(0);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[contract] AgentCore contract server listening on http://0.0.0.0:${PORT} (per-user session mode)`,
  );
  console.log(
    "[contract] Endpoints: GET /ping, POST /invocations {action: chat|status|warmup|cron}",
  );

  // Prepare the serving child before any user resources are acquired.
  v2Lifecycle.start().catch((err) => {
    console.error(`[contract] Common bootstrap failed: ${err.message}`);
  });
});
