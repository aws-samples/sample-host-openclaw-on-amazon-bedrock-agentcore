#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const crypto = require("node:crypto");
const { randomUUID } = require("node:crypto");
const WebSocket = require("../bridge/node_modules/ws");

const [url, token, configPath] = process.argv.slice(2);
if (!url || !token || !configPath) {
  throw new Error(
    "usage: verify-gateway-scope-boundary.js <ws-url> <token> <config-path>",
  );
}

const digest = () =>
  crypto.createHash("sha256").update(fs.readFileSync(configPath)).digest("hex");
const before = digest();
const connectId = randomUUID();
const mutationId = randomUUID();
const socket = new WebSocket(url, { origin: url.replace(/^ws/, "http") });

const timeout = setTimeout(() => {
  console.error("gateway scope proof timed out");
  socket.terminate();
  process.exitCode = 1;
}, 15_000);

socket.on("message", (data) => {
  const message = JSON.parse(data.toString());
  if (message.type === "event" && message.event === "connect.challenge") {
    socket.send(
      JSON.stringify({
        type: "req",
        id: connectId,
        method: "connect",
        params: {
          minProtocol: 4,
          maxProtocol: 4,
          client: {
            id: "gateway-client",
            mode: "backend",
            version: "runtime-scope-proof",
            platform: "linux",
          },
          caps: [],
          auth: { token },
          role: "operator",
          scopes: ["operator.read", "operator.write"],
        },
      }),
    );
    return;
  }

  if (message.type === "res" && message.id === connectId) {
    if (!message.ok) {
      throw new Error(`gateway connection rejected: ${JSON.stringify(message.error)}`);
    }
    socket.send(
      JSON.stringify({
        type: "req",
        id: mutationId,
        method: "config.patch",
        params: { raw: JSON.stringify({ commands: { text: true } }) },
      }),
    );
    return;
  }

  if (message.type === "res" && message.id === mutationId) {
    clearTimeout(timeout);
    const after = digest();
    const errorText = JSON.stringify(message.error || message.payload || "");
    const rejectedForScope =
      message.ok === false && /scope|permission|unauthor|forbidden/i.test(errorText);
    const unchanged = before === after;
    console.log(
      JSON.stringify(
        {
          connectedScopes: ["operator.read", "operator.write"],
          configPatchAccepted: message.ok === true,
          rejectedForScope,
          configSha256Before: before,
          configSha256After: after,
          unchanged,
          error: message.error || null,
        },
        null,
        2,
      ),
    );
    if (!rejectedForScope || !unchanged) process.exitCode = 1;
    socket.close();
  }
});

socket.on("error", (error) => {
  clearTimeout(timeout);
  console.error(error.message);
  process.exitCode = 1;
});
