/**
 * SQLite receiver preparation for the same-process gateway preload.
 *
 * The paired preparation flags require the pinned Linux ARM64 Node runtime,
 * an unbound environment, and matching patched source hashes. Preparation
 * retains an empty receiver with backend definitions loaded; activation only
 * arms its generation, leaving worker binding deferred until acquisition.
 * Each proof rechecks the source pins and receiver state.
 */
import fs from "node:fs";
import { createHash } from "node:crypto";

export function createInvariantPrewarmer() {
  let closed = false, broker, pins, bound = false;
  const live = () => {
    if (closed) throw new Error("Gateway preparation was canceled");
  };
  const verify = () => {
    live();
    for (const pin of pins || []) {
      const file = "/usr/local/lib/node_modules/openclaw/" + pin.file;
      if (!fs.lstatSync(file).isFile() ||
          createHash("sha256").update(fs.readFileSync(file)).digest("hex") !== pin.patchedSha256) {
        throw new Error("Gateway preparation source hash mismatch");
      }
    }
  };
  return {
    async prepare(seed, launcherUrl, guarded, importModule) {
      const receiver = seed.OPENCLAW_AGENTCORE_SQLITE_RECEIVER_PREPARE;
      const backend = seed.OPENCLAW_AGENTCORE_SQLITE_BACKEND_PREPARE;
      if (receiver === undefined && backend === undefined) return;
      if (receiver !== "1" || backend !== "1") throw new Error("Invalid SQLite preparation controls");
      if (process.platform !== "linux" || process.arch !== "arm64" || process.version !== "v24.21.0" ||
          Object.hasOwn(seed, "USER_ID") || Object.hasOwn(seed, "INTERNAL_USER_ID")) {
        throw new Error("SQLite preparation requires the pinned unbound runtime");
      }
      pins = JSON.parse(fs.readFileSync(new URL("./patches/runtime-v2.json", import.meta.url), "utf8")).files;
      if (!Array.isArray(pins) || !pins.length) throw new Error("Missing SQLite preparation source pins");
      verify();
      broker = await guarded(() => importModule(new URL("dist/sqlite-worker-store-CpUHa3pA.mjs", launcherUrl).href));
      live();
      await guarded(() => broker.prepareUnboundSqliteReceiver({ timeoutMs: 10000 }));
      this.proof();
    },
    async bind(generation, environment, guarded) {
      live();
      if (!broker) return;
      const result = await guarded(() => broker.bindPreparedSqliteReceiver({
        generation, environment, timeoutMs: 1500,
      }));
      if (!result.armed || result.bound || !result.binding_deferred) {
        throw new Error("SQLite receiver binding was not deferred");
      }
      bound = true;
      this.proof();
    },
    proof() {
      verify();
      if (!broker) return;
      const proof = broker.sqliteReceiverPreparationState();
      if (!proof.prepared || !proof.empty || proof.pending_control || !proof.backend_definitions_prepared ||
          (bound && !proof.armed)) throw new Error("Prepared empty SQLite receiver was lost");
    },
    async close() {
      closed = true;
      await broker?.closePreparedSqliteReceiver();
    },
  };
}
