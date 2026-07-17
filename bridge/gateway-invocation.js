"use strict";

const { createHash, randomUUID } = require("node:crypto");
const { GATEWAY_CLIENT_SCOPES } = require("./runtime-policy");

class GatewayInvocationError extends Error {
  constructor(
    message,
    {
      code,
      runId = null,
      accepted = false,
      safeToFallback = false,
      terminalStatus = null,
      cause,
    },
  ) {
    super(message, cause ? { cause } : undefined);
    this.name = "GatewayInvocationError";
    this.code = code;
    this.runId = runId;
    this.accepted = accepted;
    this.safeToFallback = safeToFallback;
    this.terminalStatus = terminalStatus;
  }
}

function createGatewayRunQuarantine() {
  return { runId: null, reason: null };
}

// Process-lifetime latch: only a runtime restart can clear an unreconciled run.
const runtimeRunQuarantine = createGatewayRunQuarantine();
const MAX_ABORT_RECONCILIATION_MS = 5_000;

/**
 * Open a loopback CLI connection. Deliberately omit Origin: pinned
 * OpenClaw treats an Origin-bearing, device-less shared-token connection as a
 * browser-like client and clears its self-declared scopes.
 */
function createGatewaySocket(WebSocketConstructor, url) {
  return new WebSocketConstructor(url);
}

function buildGatewayConnectRequest({ id, token, version = "dev" }) {
  return {
    type: "req",
    id,
    method: "connect",
    params: {
      minProtocol: 4,
      maxProtocol: 4,
      client: {
        id: "cli",
        mode: "cli",
        version,
        platform: "linux",
      },
      caps: [],
      auth: { token },
      role: "operator",
      scopes: GATEWAY_CLIENT_SCOPES,
    },
  };
}

/**
 * Trust the gateway's hello, not the scopes the client asked for. Exact scope
 * equality fails closed on both silent scope loss and unexpected expansion.
 */
function assertGrantedGatewayScopes(helloPayload) {
  const granted = helloPayload?.auth?.scopes;
  const expected = GATEWAY_CLIENT_SCOPES;
  const grantedSet = Array.isArray(granted) ? new Set(granted) : null;
  const exact =
    grantedSet !== null &&
    granted.length === expected.length &&
    grantedSet.size === expected.length &&
    expected.every((scope) => grantedSet.has(scope));

  if (!exact) {
    throw new Error(
      `Unexpected granted gateway scopes: ${JSON.stringify(granted ?? null)}; ` +
        `expected ${JSON.stringify(expected)}`,
    );
  }
  return granted;
}

function buildAgentRequest({ id, message }) {
  return {
    type: "req",
    id,
    method: "agent",
    params: {
      sessionKey: "global",
      message,
      deliver: false,
      idempotencyKey: id,
    },
  };
}

function buildAgentAbortRequest({ id, runId }) {
  return {
    type: "req",
    id,
    method: "chat.abort",
    params: {
      sessionKey: "global",
      runId,
    },
  };
}

function buildAgentWaitRequest({ id, runId, timeoutMs }) {
  return {
    type: "req",
    id,
    method: "agent.wait",
    params: { runId, timeoutMs },
  };
}

function deriveGatewayRunId({ invocationId }) {
  const match = /^po1_([0-9a-f]{64})$/.exec(invocationId || "");
  if (!match) {
    throw new Error("Invalid trusted invocation identity");
  }
  const hex = match[1].slice(0, 32);
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

function hashTrustedInvocationRequest({
  userId,
  actorId,
  channel,
  message,
}) {
  const fields = { userId, actorId, channel };
  for (const [name, value] of Object.entries(fields)) {
    if (typeof value !== "string" || value.length === 0) {
      throw new Error(`Invalid canonical invocation field: ${name}`);
    }
  }
  let canonicalMessage;
  if (typeof message === "string" && message.length > 0) {
    canonicalMessage = { kind: "text", text: message };
  } else if (
    message &&
    typeof message === "object" &&
    !Array.isArray(message) &&
    Object.keys(message).every((key) => ["text", "images"].includes(key)) &&
    typeof message.text === "string" &&
    Array.isArray(message.images) &&
    message.images.length > 0
  ) {
    const images = message.images.map((image) => {
      const valid =
        image &&
        typeof image === "object" &&
        !Array.isArray(image) &&
        Object.keys(image).every((key) =>
          ["s3Key", "contentType"].includes(key),
        ) &&
        typeof image.s3Key === "string" &&
        image.s3Key.length > 0 &&
        typeof image.contentType === "string" &&
        image.contentType.length > 0;
      if (!valid) {
        throw new Error("Invalid canonical invocation image");
      }
      return { s3Key: image.s3Key, contentType: image.contentType };
    });
    canonicalMessage = {
      kind: "structured",
      text: message.text,
      images,
    };
  } else {
    throw new Error("Invalid canonical invocation field: message");
  }
  return createHash("sha256")
    .update(
      JSON.stringify({
        version: 1,
        userId,
        actorId,
        channel,
        message: canonicalMessage,
      }),
      "utf8",
    )
    .digest("hex");
}

/**
 * Process-lifetime duplicate suppression across every local executor. Entries
 * are bound to a canonical request hash so one trusted ID cannot be replayed
 * with different work. In-flight and originating uncertain outcomes are never
 * evicted; ordinary settled outcomes (including later quarantine rejections)
 * are bounded by TTL and count.
 * Durable late-retry protection remains the router ledger's responsibility.
 */
function createTrustedInvocationRegistry({
  ttlMs = 60 * 60 * 1_000,
  maxSettledEntries = 1_000,
  maxInFlightEntries = 8,
  now = Date.now,
} = {}) {
  if (!Number.isFinite(ttlMs) || ttlMs <= 0) {
    throw new Error("Trusted invocation TTL must be positive");
  }
  if (!Number.isInteger(maxSettledEntries) || maxSettledEntries < 1) {
    throw new Error("Trusted invocation settled-entry limit must be positive");
  }
  if (!Number.isInteger(maxInFlightEntries) || maxInFlightEntries < 1) {
    throw new Error("Trusted invocation in-flight limit must be positive");
  }

  const entries = new Map();
  let sequence = 0;

  const isPinnedOutcome = (value, error) => {
    const status = value?.status;
    const code = value?.errorCode ?? error?.code;
    return status === "uncertain" || code === "UNCERTAIN_AGENT_RUN";
  };

  const prune = () => {
    const timestamp = now();
    const evictable = [];
    for (const [invocationId, entry] of entries) {
      if (!entry.settled || entry.pinned) continue;
      if (timestamp - entry.settledAt >= ttlMs) {
        entries.delete(invocationId);
        continue;
      }
      evictable.push([invocationId, entry]);
    }
    evictable.sort(
      ([, left], [, right]) =>
        left.settledAt - right.settledAt || left.sequence - right.sequence,
    );
    while (evictable.length > maxSettledEntries) {
      const [invocationId] = evictable.shift();
      entries.delete(invocationId);
    }
  };

  const invoke = ({ invocationId, requestHash, execute }) => {
    if (!/^po1_[0-9a-f]{64}$/.test(invocationId || "")) {
      return Promise.reject(
        new GatewayInvocationError("Invalid trusted invocation identity", {
          code: "INVALID_INVOCATION_IDENTITY",
          accepted: false,
          safeToFallback: false,
        }),
      );
    }
    if (!/^[0-9a-f]{64}$/.test(requestHash || "")) {
      return Promise.reject(
        new GatewayInvocationError("Invalid trusted invocation request hash", {
          code: "INVALID_INVOCATION_REQUEST_HASH",
          accepted: false,
          safeToFallback: false,
        }),
      );
    }
    if (typeof execute !== "function") {
      return Promise.reject(new TypeError("execute must be a function"));
    }

    prune();
    const existing = entries.get(invocationId);
    if (existing) {
      if (existing.requestHash !== requestHash) {
        return Promise.reject(
          new GatewayInvocationError(
            "Trusted invocation identity was reused for different work",
            {
              code: "INVOCATION_ID_CONFLICT",
              accepted: false,
              safeToFallback: false,
            },
          ),
        );
      }
      return existing.promise;
    }

    let inFlightEntries = 0;
    for (const entry of entries.values()) {
      if (!entry.settled) inFlightEntries += 1;
    }
    if (inFlightEntries >= maxInFlightEntries) {
      return Promise.reject(
        new GatewayInvocationError(
          "Runtime has reached its trusted invocation capacity",
          {
            code: "RUNTIME_OVERLOADED",
            accepted: false,
            safeToFallback: false,
          },
        ),
      );
    }

    let resolveEntry;
    let rejectEntry;
    const promise = new Promise((resolve, reject) => {
      resolveEntry = resolve;
      rejectEntry = reject;
    });
    const entry = {
      requestHash,
      promise,
      settled: false,
      pinned: false,
      settledAt: null,
      sequence: sequence++,
    };
    entries.set(invocationId, entry);

    let execution;
    try {
      execution = Promise.resolve(execute());
    } catch (error) {
      execution = Promise.reject(error);
    }
    execution.then(
      (value) => {
        entry.settled = true;
        entry.pinned = isPinnedOutcome(value, null);
        entry.settledAt = now();
        resolveEntry(value);
        prune();
      },
      (error) => {
        entry.settled = true;
        entry.pinned = isPinnedOutcome(null, error);
        entry.settledAt = now();
        rejectEntry(error);
        prune();
      },
    );
    return promise;
  };

  const getStats = () => {
    let inFlight = 0;
    let pinned = 0;
    let settledEvictable = 0;
    for (const entry of entries.values()) {
      if (!entry.settled) inFlight += 1;
      else if (entry.pinned) pinned += 1;
      else settledEvictable += 1;
    }
    return { total: entries.size, inFlight, pinned, settledEvictable };
  };

  return { invoke, getStats };
}

/**
 * Serialize gateway calls without retaining an unbounded number of message
 * closures. The running task is separate from the strict pending-task cap.
 */
function createBoundedSerialExecutor({ maxPending = 7 } = {}) {
  if (!Number.isInteger(maxPending) || maxPending < 0) {
    throw new Error("Serialized executor pending limit must be non-negative");
  }
  const pending = [];
  let running = false;

  const drain = async () => {
    if (running) return;
    const next = pending.shift();
    if (!next) return;
    running = true;
    try {
      next.resolve(await next.task());
    } catch (error) {
      next.reject(error);
    } finally {
      running = false;
      void drain();
    }
  };

  const submit = (task) => {
    if (typeof task !== "function") {
      return Promise.reject(new TypeError("task must be a function"));
    }
    if (running && pending.length >= maxPending) {
      return Promise.reject(
        new GatewayInvocationError(
          "Runtime gateway queue has reached its pending-work capacity",
          {
            code: "RUNTIME_OVERLOADED",
            accepted: false,
            safeToFallback: false,
          },
        ),
      );
    }
    return new Promise((resolve, reject) => {
      pending.push({ task, resolve, reject });
      void drain();
    });
  };

  return {
    submit,
    getStats: () => ({ running, pending: pending.length }),
  };
}

function extractAgentResponseText(payload) {
  const responsePayloads = payload?.result?.payloads;
  if (!Array.isArray(responsePayloads)) return "";
  return responsePayloads
    .map((entry) => (typeof entry?.text === "string" ? entry.text : ""))
    .filter(Boolean)
    .join("\n");
}

function classifyAgentResponse(message, requestId) {
  if (message?.type !== "res" || message.id !== requestId) return "ignore";
  if (
    message.ok === true &&
    ["accepted", "in_flight"].includes(message.payload?.status)
  ) {
    return "pending";
  }
  return "terminal";
}

function extractStreamText(payload) {
  const content =
    payload?.message?.content ??
    payload?.message ??
    payload?.text ??
    payload?.content;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((entry) => {
      if (typeof entry === "string") return entry;
      return typeof entry?.text === "string" ? entry.text : "";
    })
    .join("");
}

function invocationError(code, message, state, overrides = {}) {
  return new GatewayInvocationError(message, {
    code,
    runId: state.accepted ? state.runId : null,
    accepted: state.accepted,
    safeToFallback: !state.accepted,
    ...overrides,
  });
}

/**
 * Execute one pinned `agent` RPC as an explicit commit state machine.
 *
 * Stream events are presentation-only. The promise resolves exclusively from
 * a same-run terminal `{status:"ok"}` response. Once agent dispatch may have
 * created the run, transport failure first tries `chat.abort` on the still-owned
 * connection. A lost connection cannot prove ownership or termination and is
 * therefore quarantined as UNCERTAIN; it never returns a delta or enables a
 * second executor.
 */
function invokeGatewayAgent({
  WebSocketConstructor,
  url,
  token,
  message,
  runId,
  timeoutMs = 620_000,
  abortTimeoutMs = 5_000,
  onDelta,
  logger = console,
  quarantine = runtimeRunQuarantine,
}) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(runId || "")) {
    return Promise.reject(
      new GatewayInvocationError("Missing or invalid stable gateway run ID", {
        code: "INVALID_GATEWAY_RUN_ID",
        accepted: false,
        safeToFallback: false,
      }),
    );
  }
  if (quarantine.runId) {
    return Promise.reject(
      new GatewayInvocationError(
        "Runtime is quarantined by an earlier unreconciled agent run",
        {
          code: "AGENT_RUNTIME_QUARANTINED",
          runId: quarantine.runId,
          accepted: true,
          safeToFallback: false,
        },
      ),
    );
  }

  const reconciliationTimeoutMs = Math.max(
    1,
    Math.min(abortTimeoutMs, MAX_ABORT_RECONCILIATION_MS),
  );

  return new Promise((resolve, reject) => {
    const socket = createGatewaySocket(WebSocketConstructor, url);
    const state = {
      accepted: false,
      runId,
      settled: false,
      connectRequestId: null,
      abortRequestId: null,
      waitRequestId: null,
      abortCause: null,
    };

    let invocationTimer = null;
    let abortTimer = null;

    const clearTimers = () => {
      if (invocationTimer) clearTimeout(invocationTimer);
      if (abortTimer) clearTimeout(abortTimer);
    };

    const closeSocket = () => {
      try {
        socket.close();
      } catch {}
    };

    const fail = (error) => {
      if (state.settled) return;
      state.settled = true;
      clearTimers();
      closeSocket();
      reject(error);
    };

    const succeed = (payload) => {
      if (state.settled) return;
      state.settled = true;
      clearTimers();
      closeSocket();
      resolve({
        runId: state.runId,
        status: "ok",
        text: extractAgentResponseText(payload),
      });
    };

    const uncertain = (reason, cause) => {
      quarantine.runId = state.runId;
      quarantine.reason = reason;
      fail(
        invocationError(
          "UNCERTAIN_AGENT_RUN",
          `Accepted agent run could not be reconciled after ${reason}`,
          state,
          { safeToFallback: false, cause },
        ),
      );
    };

    const beginAbort = (reason, cause) => {
      if (state.settled || state.abortRequestId) return;
      if (!state.accepted) {
        fail(
          invocationError(
            "GATEWAY_TRANSPORT_ERROR",
            `Gateway transport failed before run acceptance (${reason})`,
            state,
            { cause },
          ),
        );
        return;
      }
      if (socket.readyState !== WebSocketConstructor.OPEN) {
        uncertain(reason, cause);
        return;
      }

      state.abortCause = reason;
      state.abortRequestId = randomUUID();
      if (invocationTimer) clearTimeout(invocationTimer);
      try {
        socket.send(
          JSON.stringify(
            buildAgentAbortRequest({
              id: state.abortRequestId,
              runId: state.runId,
            }),
          ),
        );
      } catch (error) {
        uncertain(`${reason}; exact-run abort could not be sent`, error);
        return;
      }
      abortTimer = setTimeout(() => {
        uncertain(`${reason}; exact-run abort was not confirmed`, cause);
      }, reconciliationTimeoutMs);
    };

    invocationTimer = setTimeout(() => {
      beginAbort("invocation timeout");
    }, timeoutMs);

    socket.on("open", () => {
      logger.log?.("[contract] Gateway WebSocket connected");
    });

    socket.on("message", (data) => {
      if (state.settled) return;
      let frame;
      try {
        frame = JSON.parse(data.toString());
      } catch {
        return;
      }

      if (frame.type === "event" && frame.event === "connect.challenge") {
        if (state.connectRequestId) return;
        state.connectRequestId = randomUUID();
        try {
          socket.send(
            JSON.stringify(
              buildGatewayConnectRequest({
                id: state.connectRequestId,
                token,
              }),
            ),
          );
        } catch (error) {
          fail(
            invocationError(
              "GATEWAY_TRANSPORT_ERROR",
              "Gateway connect request could not be sent",
              state,
              { cause: error },
            ),
          );
        }
        return;
      }

      if (
        frame.type === "res" &&
        state.connectRequestId &&
        frame.id === state.connectRequestId
      ) {
        if (!frame.ok) {
          fail(
            invocationError(
              "GATEWAY_CONNECT_REJECTED",
              "Gateway rejected the least-privilege connection",
              state,
            ),
          );
          return;
        }
        try {
          assertGrantedGatewayScopes(frame.payload);
        } catch (error) {
          fail(
            invocationError(
              "GATEWAY_CONNECT_REJECTED",
              "Gateway scope assertion failed",
              state,
              { cause: error },
            ),
          );
          return;
        }

        // Sending the request is the uncertainty boundary. If the transport
        // fails before the accepted acknowledgement arrives, the gateway may
        // still have started this exact run, so a second executor is forbidden.
        state.accepted = true;
        try {
          socket.send(
            JSON.stringify(
              buildAgentRequest({
                id: state.runId,
                message,
              }),
            ),
          );
        } catch (error) {
          beginAbort("agent dispatch transport failure", error);
        }
        return;
      }

      if (
        frame.type === "res" &&
        state.abortRequestId &&
        frame.id === state.abortRequestId
      ) {
        const exactRunAborted =
          frame.ok === true &&
          frame.payload?.aborted === true &&
          Array.isArray(frame.payload?.runIds) &&
          frame.payload.runIds.includes(state.runId);
        if (!exactRunAborted) {
          uncertain(`${state.abortCause}; exact-run abort was not confirmed`);
          return;
        }
        if (abortTimer) clearTimeout(abortTimer);
        state.waitRequestId = randomUUID();
        try {
          socket.send(
            JSON.stringify(
              buildAgentWaitRequest({
                id: state.waitRequestId,
                runId: state.runId,
                timeoutMs: reconciliationTimeoutMs,
              }),
            ),
          );
        } catch (error) {
          uncertain(`${state.abortCause}; terminal reconciliation could not be sent`, error);
          return;
        }
        abortTimer = setTimeout(() => {
          uncertain(`${state.abortCause}; terminal settlement was not confirmed`);
        }, reconciliationTimeoutMs + 250);
        return;
      }

      if (
        frame.type === "res" &&
        state.waitRequestId &&
        frame.id === state.waitRequestId
      ) {
        const terminalAbortConfirmed =
          frame.ok === true &&
          frame.payload?.runId === state.runId &&
          ["error", "timeout"].includes(frame.payload?.status) &&
          frame.payload?.stopReason === "rpc" &&
          Number.isFinite(frame.payload?.endedAt);
        if (!terminalAbortConfirmed) {
          uncertain(`${state.abortCause}; terminal settlement was not confirmed`);
          return;
        }
        fail(
          invocationError(
            "AGENT_RUN_ABORTED",
            `Accepted agent run reached terminal abort after ${state.abortCause}`,
            state,
            { safeToFallback: false, terminalStatus: frame.payload.status },
          ),
        );
        return;
      }

      if (frame.type === "event" && frame.event === "chat") {
        const payload = frame.payload || {};
        if (payload.runId !== state.runId || payload.state !== "delta") return;
        const text = extractStreamText(payload);
        if (text && onDelta) {
          try {
            onDelta(text);
          } catch (error) {
            logger.warn?.(`[contract] onDelta callback failed: ${error.message}`);
          }
        }
        return;
      }

      const kind = classifyAgentResponse(frame, state.runId);
      if (kind === "ignore") return;
      if (!frame.ok) {
        fail(
          invocationError(
            "AGENT_TERMINAL_ERROR",
            "Agent RPC returned a terminal error",
            state,
            { safeToFallback: false, terminalStatus: "error" },
          ),
        );
        return;
      }
      if (kind === "pending") {
        return;
      }

      const status = frame.payload?.status;
      if (status === "ok") {
        succeed(frame.payload);
        return;
      }
      if (status === "error" || status === "timeout") {
        fail(
          invocationError(
            status === "error"
              ? "AGENT_TERMINAL_ERROR"
              : "AGENT_TERMINAL_TIMEOUT",
            `Agent run reached terminal status ${status}`,
            state,
            { safeToFallback: false, terminalStatus: status },
          ),
        );
        return;
      }
      fail(
        invocationError(
          "AGENT_PROTOCOL_ERROR",
          "Agent RPC returned an unknown terminal status",
          state,
          { safeToFallback: false, terminalStatus: status ?? null },
        ),
      );
    });

    socket.on("error", (error) => {
      if (state.settled) return;
      beginAbort("transport error", error);
    });

    socket.on("close", (code, reason) => {
      if (state.settled) return;
      const detail = `transport close code=${code} reason=${reason?.toString() || ""}`;
      if (state.accepted) {
        uncertain(detail);
        return;
      }
      fail(
        invocationError(
          "GATEWAY_TRANSPORT_ERROR",
          `Gateway ${detail} before run acceptance`,
          state,
        ),
      );
    });
  });
}

async function invokeWithSafeFallback({ invokePrimary, invokeFallback }) {
  try {
    const result = await invokePrimary();
    return { ...result, source: "gateway" };
  } catch (error) {
    if (
      !(error instanceof GatewayInvocationError) ||
      error.accepted ||
      !error.safeToFallback
    ) {
      throw error;
    }
    return {
      runId: null,
      status: "ok",
      text: await invokeFallback(error),
      source: "fallback",
    };
  }
}

function createGatewayRuntimeBoundary({
  getGatewayProcess = () => null,
  terminateGraceMs = 2_000,
  schedule = setTimeout,
} = {}) {
  let quarantine = null;
  let killTimer = null;

  const quarantineRuntime = (error) => {
    if (quarantine) return quarantine;
    quarantine = {
      runId: error.runId ?? null,
      code: error.code,
      reason: error.message,
      quarantinedAt: Date.now(),
    };

    let gatewayProcess = null;
    try {
      gatewayProcess = getGatewayProcess();
    } catch {}
    const running =
      gatewayProcess &&
      gatewayProcess.exitCode === null &&
      gatewayProcess.signalCode === null;
    if (running) {
      try {
        gatewayProcess.kill("SIGTERM");
      } catch {}
      killTimer = schedule(() => {
        if (
          gatewayProcess.exitCode === null &&
          gatewayProcess.signalCode === null
        ) {
          try {
            gatewayProcess.kill("SIGKILL");
          } catch {}
        }
      }, terminateGraceMs);
      killTimer?.unref?.();
    }
    return quarantine;
  };

  const invoke = async ({ invokePrimary, invokeFallback }) => {
    if (quarantine) {
      throw new GatewayInvocationError(
        "Runtime is quarantined by an earlier unreconciled agent run",
        {
          code: "AGENT_RUNTIME_QUARANTINED",
          runId: quarantine.runId,
          accepted: true,
          safeToFallback: false,
        },
      );
    }
    try {
      return await invokeWithSafeFallback({ invokePrimary, invokeFallback });
    } catch (error) {
      if (
        error instanceof GatewayInvocationError &&
        ["UNCERTAIN_AGENT_RUN", "AGENT_RUNTIME_QUARANTINED"].includes(
          error.code,
        )
      ) {
        quarantineRuntime(error);
      }
      throw error;
    }
  };

  return {
    invoke,
    getQuarantine: () => quarantine,
  };
}

module.exports = {
  GatewayInvocationError,
  createGatewayRunQuarantine,
  createGatewaySocket,
  buildGatewayConnectRequest,
  assertGrantedGatewayScopes,
  buildAgentRequest,
  buildAgentAbortRequest,
  buildAgentWaitRequest,
  deriveGatewayRunId,
  hashTrustedInvocationRequest,
  createTrustedInvocationRegistry,
  createBoundedSerialExecutor,
  extractAgentResponseText,
  classifyAgentResponse,
  invokeGatewayAgent,
  invokeWithSafeFallback,
  createGatewayRuntimeBoundary,
};
