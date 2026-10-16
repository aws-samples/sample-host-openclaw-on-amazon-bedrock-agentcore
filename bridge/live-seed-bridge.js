/**
 * Connect live gateway seed ownership to the Bridge restore lifecycle.
 *
 * Preparation uses a shared gateway token and neutral environment. Adoption
 * requires the config, child layout and environment to remain eligible, then
 * runs owner binding inside the workspace restore's absence-proof callback.
 * Activation checks the committed restore, generation and bound environment.
 *
 * An ineligible seed can use the cold path only before binding and after its
 * unused inventory has been removed. A successful bind is recorded before
 * later checks so cleanup cannot discard adopted state if those checks fail.
 * Stop remains terminal even while token lookup or owner creation is pending.
 *
 * With coldStandby, bootstrap also prepares the cold-path gateway before the
 * seed writes its files, so a user with saved state does not wait for a fresh
 * OpenClaw module load. The standby is prepared against an empty state dir
 * (checked), waits for activation exactly like a cold gateway prepared after
 * binding, is promoted only after the unused seed is gone, and is stopped once
 * the seed is adopted. A standby that fails before promotion is replaced by a
 * freshly prepared gateway.
 */
const fs = require("node:fs");
const path = require("node:path");
const { isDeepStrictEqual } = require("node:util");
const NEUTRAL_ENV_KEYS = ["PATH", "NODE_PATH", "LANG", "TZ", "AWS_REGION", "AWS_DEFAULT_REGION",
  "AWS_EC2_METADATA_DISABLED", "CI", "DO_NOT_TRACK", "NO_COLOR", "S3_USER_FILES_BUCKET",
  "SUBAGENT_BEDROCK_MODEL_ID", "EVENTBRIDGE_SCHEDULE_GROUP", "IDENTITY_TABLE_NAME",
  "CRON_LAMBDA_ARN", "EVENTBRIDGE_ROLE_ARN", "CRON_LEAD_TIME_MINUTES"];

async function readGatewayToken({ SecretsManagerClient, GetSecretValueCommand, env = process.env }) {
  if (!env.GATEWAY_TOKEN_SECRET_ID) return null;
  const client = new SecretsManagerClient({ region: env.AWS_REGION || "us-west-2", maxAttempts: 1 });
  try {
    const result = await client.send(new GetSecretValueCommand({ SecretId: env.GATEWAY_TOKEN_SECRET_ID }));
    return typeof result.SecretString === "string" && result.SecretString ? result.SecretString : null;
  } finally {
    client.destroy();
  }
}

function createLiveSeedBridge({
  createOwner, createCold, render, readToken, stateStorage, workspaceSync,
  neutralEnv, onChild, onDiscard, inspectLocalEligibility, env = process.env,
  cwd = process.cwd(), port = 18789, coldStandby = false, log = () => {},
}) {
  const home = env.HOME || "/root";
  const paths = stateStorage.resolvePaths({ homeDir: home });
  const layout = Object.freeze({
    home, stateDir: paths.stateDir, configPath: path.join(paths.stateDir, "openclaw.json"),
    workspaceDir: paths.workspaceDir, cwd,
  });
  const neutralValues = (input) => Object.fromEntries(NEUTRAL_ENV_KEYS
    .filter((key) => input[key] !== undefined).map((key) => [key, input[key]]));
  const seedEnv = neutralValues(neutralEnv);
  let phase = "new", owner, cold, seed, common, bound, activationEnv, closing, standby;
  let stopped = false, stopReason;
  const checkRunning = () => {
    if (stopped) throw Object.assign(new Error("Live seed was stopped"), { code: "LIVE_SEED_STOPPED" });
  };
  const checkChild = () => {
    if (!owner?.child || owner.child.pid !== seed?.pid ||
        owner.child.exitCode !== null || owner.child.signalCode !== null) {
      throw new Error("Live seed child is unavailable");
    }
    const expected = {
      ...layout, controlDir: "/tmp/live-gateway-seed-control", port,
      pid: seed.pid, seedId: seed.seedId,
    };
    if (!isDeepStrictEqual(owner.layout, expected)) throw new Error("Live seed layout changed");
  };
  function assertFiles() {
    checkChild();
    let config, agentsText;
    try {
      config = JSON.parse(fs.readFileSync(layout.configPath, "utf8"));
      agentsText = fs.readFileSync(path.join(paths.stateDir, "AGENTS.md"), "utf8");
    }
    catch (error) {
      if (error instanceof SyntaxError || error.code === "ENOENT") {
        throw Object.assign(new Error("Live seed config mismatch"), { code: "LIVE_SEED_CONFIG_MISMATCH" });
      }
      throw error;
    }
    if (!isDeepStrictEqual(config, common.config) || agentsText !== common.agentsText) {
      throw Object.assign(new Error("Live seed config mismatch"), { code: "LIVE_SEED_CONFIG_MISMATCH" });
    }
  }
  async function retireOwner(reason) {
    if (!owner) return { status: "stopped", cleanupConfirmed: true, seedInventoryRemoved: false };
    if (!closing) {
      onDiscard?.(owner.child);
      closing = Promise.resolve().then(() => owner.stop(reason)).then((result) => {
        if (result?.status !== "stopped" || result.cleanupConfirmed !== true ||
            (bound && result.seedInventoryRemoved !== false)) {
          throw new Error("Live seed cleanup is unconfirmed");
        }
        return result;
      });
      closing.catch(() => {});
    }
    return closing;
  }
  function discardStandby() {
    const current = standby;
    standby = null;
    try { current?.stop(); } catch {}
  }
  // The seed owner requires this same layout (nothing, or one empty workspace dir).
  function stateDirUnused() {
    if (!fs.existsSync(paths.stateDir)) return true;
    const names = fs.readdirSync(paths.stateDir);
    return names.length === 0 || (names.length === 1 && names[0] === "workspace" &&
      fs.lstatSync(path.join(paths.stateDir, "workspace")).isDirectory() &&
      fs.readdirSync(path.join(paths.stateDir, "workspace")).length === 0);
  }
  async function prepareStandby() {
    if (!coldStandby) return;
    const started = Date.now();
    const candidate = createCold({ standby: true });
    standby = candidate;
    try {
      await candidate.prepare();
    } catch {
      if (standby === candidate) discardStandby();
      else try { candidate.stop(); } catch {}
      checkRunning();
      log("[live-seed] cold standby unavailable; a saved-state start will prepare a fresh gateway");
      return;
    }
    checkRunning();
    if (!stateDirUnused()) {
      discardStandby();
      throw new Error("Cold standby gateway wrote state before the live seed");
    }
    let rssKb = "unknown";
    try {
      const match = /^VmRSS:\s+(\d+) kB$/m.exec(fs.readFileSync(`/proc/${candidate.child.pid}/status`, "utf8"));
      if (match) rssKb = match[1];
    } catch {}
    log(`[live-seed] cold standby prepared elapsedMs=${Date.now() - started} rssKb=${rssKb}`);
  }
  async function stop(reason = "shutdown") {
    // Terminal even while token lookup or owner creation is still pending.
    if (!stopped) {
      stopped = true;
      stopReason = reason;
      phase = "stopped";
    }
    discardStandby();
    if (cold) return cold.stop();
    return retireOwner(stopReason);
  }
  // Phase telemetry must never interrupt activation.
  const report = (onPhase, name, details) => { try { onPhase?.(name, details); } catch {} };
  async function prepareCold(check = () => {}, onPhase) {
    checkRunning();
    if (bound) throw new Error("A bound live seed cannot fall back");
    if (owner) {
      report(onPhase, "seed_stop_start");
      const retired = await retireOwner("discard");
      checkRunning();
      if (retired.seedInventoryRemoved !== true) throw new Error("Unused live seed state remains");
      report(onPhase, "seed_stopped");
    }
    check();
    checkRunning();
    report(onPhase, "cold_prepare_start");
    let source = "fresh";
    const candidate = standby;
    standby = null;
    if (candidate) {
      try {
        // Already acknowledged: resolves at once, or rejects if the standby failed since.
        await candidate.prepare();
        checkRunning();
        cold = candidate;
        source = "standby";
        // Bridge listeners now own the promoted child, as for a freshly spawned one.
        onChild(cold.child);
      } catch (error) {
        try { candidate.stop(); } catch {}
        if (error?.code === "LIVE_SEED_STOPPED") throw error;
        source = "fresh_after_standby";
      }
    }
    if (!cold) {
      cold = createCold();
      await cold.prepare();
    }
    checkRunning();
    report(onPhase, "cold_prepare_ack", { source });
    check();
    phase = "cold";
    common = null;
    return { status: "cold" };
  }
  async function prepare() {
    checkRunning();
    if (phase !== "new") throw new Error("Live seed preparation was already attempted");
    phase = "preparing";
    if ((env.AGENTCORE_GATEWAY_URL || "").trim()) return prepareCold();
    const token = await readToken();
    checkRunning();
    if (!token) return prepareCold();
    common = render(token);
    if (common.config.mcp) return prepareCold();
    // Before the seed writes its config/AGENTS.md, so the standby sees the empty state dir a
    // cold gateway prepared after binding would see.
    await prepareStandby();
    checkRunning();
    owner = await createOwner({
      seedRoot: "/tmp/live-gateway-seed-control", home, cwd, port,
    });
    try {
      // The catch path stops a late-created owner before prepare can start it.
      checkRunning();
      seed = await owner.prepare({
        renderedConfig: common.config, neutralEnv: seedEnv, gatewayToken: token,
        agentsText: common.agentsText,
      });
      checkRunning();
      if (seed?.status !== "prepared" || seed.prepared !== true ||
          !Number.isSafeInteger(seed.pid) || seed.pid <= 0) {
        return prepareCold();
      }
      checkChild();
      assertFiles();
      onChild(owner.child);
      // Normal Bridge listeners now own subsequent lines. Do not replay arbitrary pipe content.
      try { owner.handoverEarlyDiagnostics?.(); } catch {}
      phase = "prepared";
      return { status: "prepared", pid: seed.pid };
    } catch (error) {
      // An interface mismatch cannot manufacture a retained child or successful seed.
      await stop("discard");
      throw error;
    }
  }
  async function restoreForBinding({ lifecycle, scope, namespace, channel, token, boundEnv, waitMs, onPhase }) {
    scope.checkpoint();
    checkRunning();
    if (cold) return { status: "cold" };
    if (phase !== "prepared") throw new Error("Live seed is not prepared");
    const current = lifecycle.snapshot();
    const binding = current.binding;
    if (!binding || current.generation !== scope.generation) throw new Error("Live seed binding mismatch");
    const check = () => {
      scope.checkpoint();
      checkRunning();
      const now = lifecycle.snapshot();
      if (now.state !== "BINDING" || now.binding !== binding ||
          now.generation !== scope.generation || !now.resources.includes("credentials") ||
          namespace !== binding.actorId.replace(/:/g, "_") ||
          env.USER_ID !== binding.actorId || env.INTERNAL_USER_ID !== binding.userId) {
        throw new Error("Live seed binding changed");
      }
      lifecycle.assertBinding(binding);
    };
    let expected;
    try {
      check();
      expected = render(token);
      let eligible = !(env.AGENTCORE_GATEWAY_URL || "").trim() && token &&
        isDeepStrictEqual(expected, common) && isDeepStrictEqual(neutralValues(boundEnv), seedEnv);
      if (eligible) {
        try { assertFiles(); }
        catch (error) {
          if (error.code !== "LIVE_SEED_CONFIG_MISMATCH") throw error;
          eligible = false;
        }
      }
      if (eligible && inspectLocalEligibility() !== true) eligible = false;
      if (!eligible) {
        report(onPhase, "seed_ineligible", { reason: "bridge_check" });
        return await prepareCold(check, onPhase);
      }
      report(onPhase, "seed_restore_start");
      let result;
      const restore = workspaceSync.restoreAbsentSeed(namespace, {
        lifecycle, scope,
        bindSeed: async (absenceProof) => {
          check();
          checkChild();
          const late = { USER_ID: binding.actorId, INTERNAL_USER_ID: binding.userId, CHANNEL: channel };
          for (const key of ["AWS_CONFIG_FILE", "AWS_SDK_LOAD_CONFIG", "AWS_SHARED_CREDENTIALS_FILE"]) {
            if (boundEnv[key] !== undefined) late[key] = boundEnv[key];
          }
          const reply = await owner.bind({
            generation: scope.generation, actorId: binding.actorId, userId: binding.userId,
            channel, renderedConfig: expected.config, gatewayToken: token,
            boundEnv: late, absenceProof,
          });
          if (reply?.status === "bound" && reply.bound === true) {
            // Never discard state after a successful bind, even if a later check fails.
            bound = { ...reply, actorId: binding.actorId, userId: binding.userId };
            if (reply.seedId !== seed.seedId || reply.pid !== seed.pid ||
                reply.generation !== scope.generation || reply.userReady !== false) {
              throw new Error("Live seed binding acknowledgement mismatch");
            }
            checkChild();
          }
          check();
          return reply;
        },
      }).then((value) => { result = value; return value; });
      await workspaceSync.awaitRestore(restore, waitMs);
      check();
      if (result?.status === "ineligible") {
        const reason = typeof result.reason === "string" && /^[a-z_]{1,64}$/.test(result.reason)
          ? result.reason : "unknown";
        report(onPhase, "seed_ineligible", { reason });
        return await prepareCold(check, onPhase);
      }
      if (result?.status !== "adopted" || !bound) throw new Error("Live seed restore did not commit");
      workspaceSync.assertRestoreReady();
      activationEnv = { ...boundEnv };
      phase = "adopted";
      discardStandby();
      return result;
    } catch (error) {
      try { await stop(bound ? "shutdown" : "abort"); } catch {}
      throw error;
    }
  }
  async function activate(input) {
    checkRunning();
    if (cold) return cold.activate(input);
    if (input.signal?.aborted) throw input.signal.reason;
    checkChild();
    if (phase !== "adopted" || input.generation !== bound.generation ||
        input.binding?.actorId !== bound.actorId || input.binding?.userId !== bound.userId ||
        !isDeepStrictEqual(input.env, activationEnv)) throw new Error("Live seed activation changed");
    workspaceSync.assertRestoreReady();
    assertFiles();
  }
  return {
    prepare, restoreForBinding, activate, stop, assertFiles,
    get child() { return cold ? cold.child : owner?.child; },
    get childBinding() {
      return cold ? cold.childBinding : bound ? Object.freeze({
        pid: bound.pid, actor_id: bound.actorId, user_id: bound.userId, generation: bound.generation,
      }) : null;
    },
    get prepared() { return phase === "prepared"; },
    get standbyPrepared() { return !!standby; },
    get adopted() { return phase === "adopted"; },
  };
}

module.exports = { readGatewayToken, createLiveSeedBridge };
