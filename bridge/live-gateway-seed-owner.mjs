/**
 * Parent-side owner of one detached, unbound live gateway process.
 *
 * Preparation installs the common config in an empty state directory,
 * starts the pinned CLI, and records its process identity, layout and state
 * inventory after the child acknowledges heartbeat quiescence. Binding
 * adopts that same process for one generation; the acknowledgement does not
 * mark it ready for user traffic.
 *
 * Cleanup distinguishes disposable seed state from potentially adopted state.
 * Unused inventory is removed only after confirmed shutdown and content
 * checks, allowing the expected SQLite checkpoint and lease-release changes.
 * Once a bind has been sent, a missing acknowledgement leaves adoption
 * uncertain and forbids cold fallback.
 */
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import { check, keys, equal, version, uuid, processWitness, listeners, privateWitness,
  configValid, prepareEnv, validateBind, sleep, safeReason, agentsValid,
  LIVE_EAGER_COMPILE_FLAG, readLiveEagerCompileFlag,
  liveGatewayExecArgv } from "./live-gateway-seed-common.mjs";

export function createLiveGatewaySeedOwner(options) {
  const { seedRoot, home, cwd, port, node = process.execPath,
    cli = "/usr/local/lib/node_modules/openclaw/openclaw.mjs",
    timeoutMs = 120000, onEvent } = options;
  check(typeof home === "string" && path.isAbsolute(home) && fs.statSync(home).isDirectory() &&
    path.isAbsolute(seedRoot) && seedRoot !== "/" && path.isAbsolute(cwd) &&
    fs.statSync(cwd).isDirectory() && Number.isInteger(port) && port > 1024 && port < 65536 &&
    Number.isInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 180000, "owner_options");
  check(process.platform === "linux" && process.arch === "arm64" &&
    process.version === "v24.21.0" && node === process.execPath &&
    cli === "/usr/local/lib/node_modules/openclaw/openclaw.mjs", "owner_runtime");
  const seedId = randomUUID();
  const stateRoot = path.join(home, ".openclaw");
  const overlap = (a, b) => a === b || a.startsWith(b + "/") || b.startsWith(a + "/");
  check(!overlap(seedRoot, stateRoot) && !overlap(seedRoot, "/mnt/workspace") &&
    !overlap(stateRoot, "/mnt/workspace"), "control_layout");
  let state = "new", child, witness, rootWitness, prepared, installed = false, bindSent = false, closing;
  let stateWitness, initialState, ownedPrepared = false;
  let ownedInventory, ownedDatabase, inventoryProtected = false, stoppingAt;
  let layout = null, spawning, spawnFailed = false;
  const databaseFile = "state/openclaw.sqlite";
  const databaseFiles = new Set([databaseFile, databaseFile + "-wal", databaseFile + "-shm"]);
  const lockDirectory = `tmp/openclaw-${process.getuid()}`;
  const seedDirectories = new Set(["", "workspace", "media", "media/outbound", "media/playback-transcode",
    "state", "tmp", "tmp/agent-cli", "tmp/node-workspace-transfer", lockDirectory]);
  const seedFiles = new Set(["AGENTS.md", "openclaw.json", "config-journal-fingerprint.key",
    "tmp/agent-cli/openclaw", ...databaseFiles]);
  const lockFile = file => path.dirname(file) === lockDirectory &&
    /^(?:gateway\.(?:[a-f0-9]+|state)\.lock(?:\.sqlite)?|device-identity\.[a-f0-9]+\.lock\.sqlite)$/.test(path.basename(file));
  const digest = value => createHash("sha256").update(value).digest("hex");
  function inventory() {
    const entries = {};
    const visit = relative => {
      const file = path.join(stateRoot, relative), stat = fs.lstatSync(file);
      check(stat.uid === process.getuid() && (stat.isDirectory() || stat.isFile() && stat.nlink === 1),
        "inventory_not_owned");
      check(stat.isDirectory() ? seedDirectories.has(relative) : seedFiles.has(relative) || lockFile(relative),
        "inventory_not_owned");
      entries[relative] = { dev: stat.dev, ino: stat.ino, mode: stat.mode, uid: stat.uid, gid: stat.gid,
        directory: stat.isDirectory(), ...stat.isFile() ?
          { size: stat.size, sha256: digest(fs.readFileSync(file)) } : {} };
      if (stat.isDirectory()) for (const name of fs.readdirSync(file).sort())
        visit(path.join(relative, name));
    };
    visit("");
    return entries;
  }
  function databaseInventory() {
    if (!fs.existsSync(path.join(stateRoot, databaseFile))) return null;
    // Verify a private copy without changing the live database's WAL or shared memory.
    const directory = fs.mkdtempSync(path.join(seedRoot, "inventory-"));
    let db;
    try {
      for (const suffix of ["", "-wal"]) {
        const source = path.join(stateRoot, databaseFile + suffix);
        if (fs.existsSync(source)) fs.copyFileSync(source, path.join(directory, "state.sqlite" + suffix));
      }
      db = new DatabaseSync(path.join(directory, "state.sqlite"), { readOnly: true });
      const schema = db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name").all();
      const tables = {}, special = {};
      for (const { name, type } of schema) {
        if (type !== "table") continue;
        const rows = db.prepare('SELECT * FROM "' + name.replaceAll('"', '""') + '"').all();
        if (name === "gateway_boot_lifecycle" || name === "state_leases") special[name] = rows;
        else tables[name] = digest(JSON.stringify(rows.map(row => JSON.stringify(row)).sort()));
      }
      return { schema: digest(JSON.stringify(schema)), tables, ...special };
    } finally {
      try { db?.close(); } finally { fs.rmSync(directory, { recursive: true, force: true }); }
    }
  }
  function validateDatabaseShutdown(current) {
    if (!ownedDatabase) { check(current === null, "database_added"); return; }
    check(current && current.schema === ownedDatabase.schema &&
      equal(current.tables, ownedDatabase.tables), "database_content_changed");
    const boots = ownedDatabase.gateway_boot_lifecycle, leases = ownedDatabase.state_leases;
    check(boots?.length === 1 && boots[0].pid === child.pid && boots[0].completed_at_ms === null &&
      boots[0].outcome === null && boots[0].reason === null && leases?.length === 1 &&
      leases[0].scope === "gateway-owner" && leases[0].lease_key === "global" &&
      JSON.parse(leases[0].payload_json)?.owner?.pid === child.pid, "database_owner_changed");
    const ended = current.gateway_boot_lifecycle?.[0];
    check(current.gateway_boot_lifecycle?.length === 1 && current.state_leases?.length === 0 &&
      Number.isSafeInteger(ended.completed_at_ms) && ended.completed_at_ms >= stoppingAt &&
      ended.completed_at_ms <= Date.now() &&
      equal({ ...ended, completed_at_ms: null, outcome: null, reason: null }, { ...boots[0] }) &&
      ended.outcome === "clean_stop" && ended.reason === "stop (SIGTERM)", "database_shutdown_changed");
  }
  function removeUnusedInventory() {
    const after = inventory();
    for (const [file, item] of Object.entries(after)) {
      const original = ownedInventory[file];
      check(original, "inventory_added");
      // Shutdown checkpoints SQLite and removes the owner's lease; all other contents remain exact.
      if (databaseFiles.has(file)) {
        check(!item.directory && item.mode === original.mode && item.uid === original.uid && item.gid === original.gid &&
          (file !== databaseFile || item.dev === original.dev && item.ino === original.ino), "database_replaced");
      } else check(equal(item, original), "inventory_changed");
    }
    for (const file of Object.keys(ownedInventory)) if (!Object.hasOwn(after, file))
      check(file === databaseFile + "-wal" || file === databaseFile + "-shm" ||
        file === "tmp/node-workspace-transfer" || lockFile(file) && file.endsWith(".lock"), "inventory_missing");
    validateDatabaseShutdown(databaseInventory());
    const final = inventory();
    check(equal(final, after), "inventory_changed");
    for (const file of Object.keys(final).sort((a, b) => b.length - a.length)) {
      check(!inventoryProtected, "inventory_protected");
      const absolute = path.join(stateRoot, file);
      if (final[file].directory) fs.rmdirSync(absolute);
      else fs.unlinkSync(absolute);
    }
    if (initialState) {
      fs.mkdirSync(stateRoot, { mode: initialState.mode });
      if (initialState.workspace) fs.mkdirSync(path.join(stateRoot, "workspace"), { mode: initialState.workspace });
    }
  }
  function protectInventory() { inventoryProtected = true; }
  const expectedLayout = () => ({ home, stateDir: stateRoot,
    configPath: path.join(stateRoot, "openclaw.json"), workspaceDir: path.join(stateRoot, "workspace"),
    cwd, controlDir: seedRoot, port, pid: child.pid, seedId });
  const pending = new Map();
  const emit = event => { try { onEvent?.({ event, seedId, pid: child?.pid ?? null }); } catch {} };
  const sameProcess = () => {
    const current = child && processWitness(child.pid);
    check(current && witness && current.start === witness.start && current.group === witness.group &&
      current.state !== "Z", "producer_changed");
  };
  function call(type, payload) {
    check(!closing && state === (type === "prepare" ? "preparing" : "binding"), "owner_stopped");
    sameProcess();
    const requestId = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error("operation_deadline"));
      }, timeoutMs);
      pending.set(requestId, { resolve, reject, timer });
      // Sending can succeed before its callback or the bound acknowledgement arrives.
      if (type === "bind") bindSent = true;
      const delivered = error => {
        if (error) { clearTimeout(timer); pending.delete(requestId); reject(new Error("ipc_closed")); }
      };
      try { child.send({ type, version, seedId, requestId, payload }, delivered); }
      catch { delivered(new Error("ipc_closed")); }
    });
  }
  async function stop(reason = "shutdown") {
    check(["discard", "abort", "shutdown", "invalid_bind", "protocol_failure", "native_failure"].includes(reason),
      "stop_reason");
    if (closing) return closing;
    closing = (async () => {
      state = "stopping";
      for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("owner_stopped")); }
      pending.clear();
      try { await spawning; } catch {}
      if (ownedPrepared && !inventoryProtected) {
        try { check(equal(inventory(), ownedInventory), "inventory_changed"); }
        catch { protectInventory(); }
      }
      stoppingAt = Date.now();
      if (child && child.exitCode === null && child.signalCode === null) {
        try { sameProcess(); child.kill("SIGTERM"); } catch {}
        const end = Date.now() + 15000;
        while (child.exitCode === null && child.signalCode === null && Date.now() < end) await sleep(25);
      }
      const group = () => fs.readdirSync("/proc").filter(name => /^\d+$/.test(name)).flatMap(name => {
        try { const item = processWitness(Number(name)); return item.group === witness?.group && item.state !== "Z" ? [item] : []; }
        catch { return []; }
      });
      // Only this fresh detached process group belongs to the owner.
      const survivors = child ? group() : [];
      let forced = false, replaced = false;
      try { replaced = processWitness(child.pid).start !== witness.start; } catch {}
      if (survivors.length) {
        if (!replaced) try { process.kill(-witness.group, "SIGKILL"); forced = true; } catch {}
        await sleep(100);
      }
      const confirmed = (!child || witness || spawnFailed) && !replaced && !forced &&
        (!child || group().length === 0) &&
        !listeners().some(row => row.port === port);
      let removed = false, controlRemoved = false;
      if (confirmed && stateWitness && ownedPrepared && !bindSent && !inventoryProtected) {
        try {
          check(equal(privateWitness(stateRoot, true), stateWitness), "state_root_replaced");
          removeUnusedInventory();
          removed = true;
        } catch { protectInventory(); }
      }
      if (confirmed && rootWitness) {
        try {
          check(equal(privateWitness(seedRoot, true), rootWitness), "seed_root_replaced");
          fs.rmSync(seedRoot, { recursive: true });
          controlRemoved = true;
        } catch {}
      }
      state = confirmed ? "stopped" : "cleanup_failed";
      emit(state);
      return { status: state, cleanupConfirmed: confirmed, seedInventoryRemoved: removed, controlRemoved, forced,
        adoptionStatus: installed ? "bound" : bindSent ? "unknown" : "not_attempted" };
    })();
    return closing;
  }
  async function fallback(reason) {
    const cleanup = await stop("native_failure");
    if (bindSent) return { status: "failed", seedId, pid: child?.pid ?? null,
      bound: installed, userReady: false, reason, coldFallbackAllowed: false,
      adoptionStatus: installed ? "bound" : "unknown", cleanup };
    return { status: "fallback", seedId, pid: child?.pid ?? null, prepared: false, bound: false,
      userReady: false, reason, cleanup,
      coldFallbackAllowed: cleanup.cleanupConfirmed && cleanup.seedInventoryRemoved };
  }
  return {
    get child() { return child ?? null; },
    get layout() { return layout; },
    async prepare(input) {
      try {
        check(state === "new", "prepare_consumed");
        state = "preparing";
        keys(input, ["renderedConfig", "neutralEnv", "gatewayToken", "agentsText"]);
        configValid(input.renderedConfig, input.gatewayToken, port);
        agentsValid(input.agentsText);
        input = structuredClone(input);
        check(!listeners().some(row => row.port === port), "port_busy");
        const env = prepareEnv(input.neutralEnv, home);
        const liveEagerCompile = readLiveEagerCompileFlag(env[LIVE_EAGER_COMPILE_FLAG]);
        privateWitness(home, true);
        fs.mkdirSync(seedRoot, { mode: 0o700 });
        rootWitness = privateWitness(seedRoot, true);
        if (fs.existsSync(stateRoot)) {
          const stat = fs.lstatSync(stateRoot);
          check(stat.isDirectory() && stat.uid === process.getuid(), "initial_state_layout");
          const names = fs.readdirSync(stateRoot);
          check(names.length === 0 || (names.length === 1 && names[0] === "workspace"), "initial_state_not_empty");
          initialState = { mode: stat.mode & 0o777 };
          if (names.length) {
            const directory = path.join(stateRoot, "workspace"), item = fs.lstatSync(directory);
            check(item.isDirectory() && item.uid === process.getuid() && fs.readdirSync(directory).length === 0,
              "initial_workspace_not_empty");
            initialState.workspace = item.mode & 0o777;
          }
          fs.chmodSync(stateRoot, 0o700);
        } else fs.mkdirSync(stateRoot, { mode: 0o700 });
        stateWitness = privateWitness(stateRoot, true);
        if (!initialState?.workspace) fs.mkdirSync(path.join(stateRoot, "workspace"), { mode: 0o700 });
        fs.writeFileSync(env.OPENCLAW_CONFIG_PATH, JSON.stringify(input.renderedConfig), { mode: 0o600, flag: "wx" });
        fs.writeFileSync(path.join(stateRoot, "AGENTS.md"), input.agentsText, { mode: 0o600, flag: "wx" });
        prepared = structuredClone(input);
        child = spawn(node, [...liveGatewayExecArgv(env[LIVE_EAGER_COMPILE_FLAG]), cli,
          "gateway", "run", "--port", String(port), "--bind", "loopback"], {
          cwd, env, detached: true, stdio: ["ignore", "pipe", "pipe", "ipc"],
        });
        spawning = new Promise((resolve, reject) => {
          child.once("spawn", () => {
            try { witness = processWitness(child.pid); resolve(); } catch (error) { reject(error); }
          });
          child.once("error", error => { spawnFailed = !child.pid; reject(error); });
        });
        await spawning;
        check(!closing && state === "preparing", "owner_stopped");
        // Keep ordinary CLI pipes drained even before Bridge attaches its normal listeners.
        child.stdout.resume();
        child.stderr.resume();
        child.on("message", reply => {
          try {
            keys(reply, ["type", "version", "seedId", "requestId", "status", "pid", "generation", "reason", "proof"]);
            check(reply.type === "result" && reply.version === version && reply.seedId === seedId &&
              reply.pid === child.pid && uuid(reply.requestId), "reply_identity");
            check(["prepared", "bound", "fallback", "stopping"].includes(reply.status) &&
              (reply.generation === null || uuid(reply.generation)) &&
              (reply.reason === null || /^[a-z_]{1,64}$/.test(reply.reason)), "reply_shape");
            if (reply.status === "prepared") {
              keys(reply.proof, ["heartbeatQuiescent", "layout",
                ...(liveEagerCompile ? ["liveEagerCompileExecArgv"] : [])]);
              check(!liveEagerCompile || reply.proof.liveEagerCompileExecArgv === true,
                "live_eager_compile_execution");
            }
            if (reply.status === "bound") {
              keys(reply.proof, ["heartbeatResponsive", "userReady", "layout"]);
              check(reply.proof.userReady === false, "reply_readiness");
            }
            if (["prepared", "bound"].includes(reply.status))
              check(equal(reply.proof.layout, expectedLayout()), "reply_layout");
            sameProcess();
            const item = pending.get(reply.requestId);
            check(item, "reply_replayed");
            pending.delete(reply.requestId); clearTimeout(item.timer); item.resolve(reply);
          } catch { void stop("protocol_failure"); }
        });
        child.once("exit", () => {
          for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("producer_exited")); }
          pending.clear();
        });
        const reply = await call("prepare", { ...input, seedRoot, home, cwd, port });
        check(!closing && state === "preparing", "owner_stopped");
        check(reply.status === "prepared" && reply.generation === null &&
          reply.proof?.heartbeatQuiescent === true, reply.reason || "not_prepared");
        check(!inventoryProtected, "inventory_protected");
        ownedInventory = inventory();
        ownedDatabase = databaseInventory();
        check(equal(inventory(), ownedInventory), "inventory_changed");
        ownedPrepared = true;
        layout = Object.freeze({ ...reply.proof.layout });
        state = "prepared"; emit("prepared");
        return { status: "prepared", seedId, pid: child.pid, prepared: true };
      } catch (error) { return fallback(safeReason(error)); }
    },
    async bind(input) {
      try {
        check(state === "prepared", "bind_consumed");
        protectInventory();
        state = "binding";
        validateBind(input, prepared);
        input = structuredClone(input);
        sameProcess();
        const reply = await call("bind", input);
        check(!closing && state === "binding", "owner_stopped");
        check(reply.status === "bound" && reply.generation === input.generation &&
          reply.proof?.heartbeatResponsive === true, reply.reason || "not_bound");
        // Bridge admission remains closed until this successful return.
        installed = true;
        state = "bound"; emit("bound");
        return { status: "bound", seedId, generation: input.generation, pid: child.pid, bound: true, userReady: false };
      } catch (error) { return fallback(safeReason(error)); }
    },
    protectInventory,
    stop,
  };
}
