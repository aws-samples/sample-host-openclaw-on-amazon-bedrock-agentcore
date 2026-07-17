#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const http = require("node:http");
const crypto = require("node:crypto");
const { randomUUID } = require("node:crypto");
const WebSocket = require("../bridge/node_modules/ws");
const gatewayInvocation = require("../bridge/gateway-invocation");

const [url, token, configPath] = process.argv.slice(2);
if (!url || !token || !configPath) {
  throw new Error(
    "usage: verify-gateway-scope-boundary.js <ws-url> <token> <config-path>",
  );
}

const digest = () =>
  crypto.createHash("sha256").update(fs.readFileSync(configPath)).digest("hex");

function extractText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) =>
      typeof part === "string"
        ? part
        : typeof part?.text === "string"
          ? part.text
          : "",
    )
    .join("");
}

function resolveModelProofUrl(config) {
  const providers = Object.values(config.models?.providers || {});
  const baseUrl = providers.find(
    (provider) => typeof provider?.baseUrl === "string",
  )?.baseUrl;
  if (!baseUrl) throw new Error("gateway proof config has no model provider baseUrl");
  const parsed = new URL(baseUrl);
  if (
    parsed.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "::1"].includes(parsed.hostname)
  ) {
    throw new Error("gateway proof model provider must be loopback HTTP");
  }
  return parsed;
}

function createModelProofServer(modelUrl, capture) {
  const responseText = "MODEL_RECEIVED_LITERAL_SLASH_STATUS";
  const abortProofInput = "hold for exact abort proof";
  const server = http.createServer((request, response) => {
    let body = "";
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
    });
    request.on("end", () => {
      try {
        const payload = JSON.parse(body);
        const userText = [...(payload.messages || [])]
          .reverse()
          .find((message) => message?.role === "user");
        capture.userTexts.push(extractText(userText?.content));
        capture.requestCount += 1;

        if (capture.userTexts.at(-1) === abortProofInput) {
          response.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
          });
          response.write(
            `data: ${JSON.stringify({
              id: "chatcmpl-abort-proof",
              object: "chat.completion.chunk",
              created: Math.floor(Date.now() / 1000),
              model: payload.model || "bedrock-agentcore",
              choices: [
                {
                  index: 0,
                  delta: {
                    role: "assistant",
                    content: "UNCOMMITTED_ABORT_PROOF_DELTA",
                  },
                  finish_reason: null,
                },
              ],
            })}\n\n`,
          );
          response.on("close", () => {
            capture.abortProofProviderClosed = true;
          });
          return;
        }

        if (payload.stream === true) {
          response.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
          });
          const base = {
            id: "chatcmpl-scope-proof",
            object: "chat.completion.chunk",
            created: Math.floor(Date.now() / 1000),
            model: payload.model || "bedrock-agentcore",
          };
          response.write(
            `data: ${JSON.stringify({
              ...base,
              choices: [
                {
                  index: 0,
                  delta: { role: "assistant", content: responseText },
                  finish_reason: null,
                },
              ],
            })}\n\n`,
          );
          response.write(
            `data: ${JSON.stringify({
              ...base,
              choices: [
                { index: 0, delta: {}, finish_reason: "stop" },
              ],
            })}\n\n`,
          );
          response.end("data: [DONE]\n\n");
          return;
        }

        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(
          JSON.stringify({
            id: "chatcmpl-scope-proof",
            object: "chat.completion",
            created: Math.floor(Date.now() / 1000),
            model: payload.model || "bedrock-agentcore",
            choices: [
              {
                index: 0,
                message: { role: "assistant", content: responseText },
                finish_reason: "stop",
              },
            ],
            usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
          }),
        );
      } catch (error) {
        response.writeHead(400, { "Content-Type": "text/plain" });
        response.end(error.message);
      }
    });
  });

  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(Number(modelUrl.port || 80), modelUrl.hostname, () => {
      server.removeListener("error", reject);
      resolve(server);
    });
  });
}

async function main() {
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const modelUrl = resolveModelProofUrl(config);
  const capture = {
    requestCount: 0,
    userTexts: [],
    abortProofProviderClosed: false,
  };
  const modelServer = await createModelProofServer(modelUrl, capture);
  const before = digest();
  const connectId = randomUUID();
  const mutationId = randomUUID();
  const connectRequest = gatewayInvocation.buildGatewayConnectRequest({
    id: connectId,
    token,
    version: "runtime-scope-proof",
  });
  const firstRunId = randomUUID();
  const cases = [
    { input: "/status", outcome: "model", id: firstRunId },
    { input: "/status", outcome: "duplicate", id: firstRunId },
    { input: "/new", outcome: "reject" },
    { input: "/reset", outcome: "reject" },
    { input: "/config set commands.text true", outcome: "model" },
    { input: "/po_file_read notes.md", outcome: "model" },
    { input: "hold for exact abort proof", outcome: "abort" },
  ].map((testCase) => {
    const id = testCase.id || randomUUID();
    return {
      ...testCase,
      id,
      request: gatewayInvocation.buildAgentRequest({
        id,
        message: testCase.input,
      }),
    };
  });
  const socket = gatewayInvocation.createGatewaySocket(WebSocket, url);
  let connectedScopes = null;
  let mutationResponse = null;
  let activeCase = null;
  let abortResponse = null;
  let abortWaitResponse = null;
  const results = [];
  let finished = false;

  const sendNextCase = () => {
    activeCase = cases[results.length] || null;
    if (!activeCase) {
      finish();
      return;
    }
    activeCase.providerRequestCountBefore = capture.requestCount;
    activeCase.configSha256Before = digest();
    socket.send(JSON.stringify(activeCase.request));
  };

  const sendAbortWhenProviderStarted = (testCase, attempts = 0) => {
    if (finished || activeCase !== testCase || testCase.abortRequestId) return;
    const providerStarted = capture.userTexts.some(
      (text) =>
        text === testCase.input || text.endsWith(`] ${testCase.input}`),
    );
    if (!providerStarted) {
      if (attempts >= 100) {
        finish(new Error("abort proof provider request did not start"));
        return;
      }
      setTimeout(() => sendAbortWhenProviderStarted(testCase, attempts + 1), 20);
      return;
    }
    testCase.abortRequestId = randomUUID();
    socket.send(
      JSON.stringify(
        gatewayInvocation.buildAgentAbortRequest({
          id: testCase.abortRequestId,
          runId: testCase.id,
        }),
      ),
    );
  };

  const finish = (error) => {
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    const after = digest();
    const errorText = JSON.stringify(
      mutationResponse?.error || mutationResponse?.payload || "",
    );
    const rejectedForScope =
      mutationResponse?.ok === false &&
      /scope|permission|unauthor|forbidden/i.test(errorText);
    const providerSaw = (input) =>
      capture.userTexts.some(
        (text) => text === input || text.endsWith(`] ${input}`),
      );
    const normalResults = results.filter((result) => result.outcome === "model");
    const duplicateResult = results.find(
      (result) => result.outcome === "duplicate",
    );
    const rejectedResults = results.filter(
      (result) => result.outcome === "reject",
    );
    const abortResult = results.find((result) => result.outcome === "abort");
    const modelInputs = [
      "/status",
      "/config set commands.text true",
      "/po_file_read notes.md",
    ];
    const rejectedInputs = ["/new", "/reset"];
    const normalAgentCompleted =
      normalResults.length === modelInputs.length &&
      normalResults.every(
        (result) =>
          result.accepted &&
          result.status === "ok" &&
          result.text === "MODEL_RECEIVED_LITERAL_SLASH_STATUS" &&
          result.finalPromptText === result.input &&
          result.providerRequestDelta === 1,
      );
    const modelSawLiteralInputs = modelInputs.every(providerSaw);
    const controlsRejected =
      rejectedResults.length === rejectedInputs.length &&
      rejectedResults.every(
        (result) =>
          result.rejected &&
          /operator\.admin|admin scope/i.test(result.error || "") &&
          result.providerRequestDelta === 0 &&
          result.configUnchangedDuring,
      ) &&
      rejectedInputs.every((input) => !providerSaw(input));
    const sessionIds = normalResults
      .map((result) => result.sessionId)
      .filter(Boolean);
    const sessionContinuous =
      sessionIds.length === modelInputs.length &&
      new Set(sessionIds).size === 1;
    const duplicateIdempotencySingleProvider =
      duplicateResult?.status === "ok" &&
      duplicateResult?.providerRequestDelta === 0 &&
      capture.userTexts.filter(
        (text) => text === "/status" || text.endsWith("] /status"),
      ).length === 1;
    const exactRunAbortConfirmed =
      abortResult?.accepted === true &&
      abortResult?.abortAccepted === true &&
      abortResult?.abortedRunIds?.includes(abortResult.id) &&
      ["error", "timeout"].includes(abortResult?.waitStatus) &&
      abortResult?.waitStopReason === "rpc" &&
      abortResult?.providerRequestDelta === 1;
    const unchanged = before === after;
    const report = {
      requestedScopes: connectRequest.params.scopes,
      connectedScopes,
      configPatchAccepted: mutationResponse?.ok === true,
      configPatchError: mutationResponse?.error || null,
      rejectedForScope,
      configSha256Before: before,
      configSha256After: after,
      unchanged,
      agentMethod: "agent",
      agentResults: results,
      normalAgentCompleted,
      controlsRejected,
      sessionContinuous,
      duplicateIdempotencySingleProvider,
      exactRunAbortConfirmed,
      abortResponse,
      abortWaitResponse,
      abortProofProviderClosed: capture.abortProofProviderClosed,
      modelRequestCount: capture.requestCount,
      modelUserTexts: capture.userTexts,
      modelSawLiteralInputs,
      error: error ? error.message : null,
    };
    console.log(JSON.stringify(report, null, 2));

    if (
      error ||
      !rejectedForScope ||
      !unchanged ||
      !normalAgentCompleted ||
      !controlsRejected ||
      !sessionContinuous ||
      !duplicateIdempotencySingleProvider ||
      !exactRunAbortConfirmed ||
      !modelSawLiteralInputs
    ) {
      process.exitCode = 1;
    }
    try {
      socket.close();
    } catch {}
    modelServer.close();
  };

  const timeout = setTimeout(() => {
    try {
      socket.terminate();
    } catch {}
    finish(new Error("gateway scope/agent proof timed out"));
  }, 60_000);

  socket.on("message", (data) => {
    try {
      const message = JSON.parse(data.toString());
      if (message.type === "event" && message.event === "connect.challenge") {
        socket.send(JSON.stringify(connectRequest));
        return;
      }

      if (message.type === "res" && message.id === connectId) {
        if (!message.ok) {
          finish(
            new Error(
              `gateway connection rejected: ${JSON.stringify(message.error)}`,
            ),
          );
          return;
        }
        connectedScopes =
          gatewayInvocation.assertGrantedGatewayScopes(message.payload);
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
        mutationResponse = message;
        sendNextCase();
        return;
      }

      if (
        message.type === "res" &&
        activeCase?.outcome === "abort" &&
        message.id === activeCase.abortRequestId
      ) {
        abortResponse = message;
        if (!message.ok) {
          finish(
            new Error(
              `exact-run abort rejected: ${JSON.stringify(message.error)}`,
            ),
          );
          return;
        }
        activeCase.abortAccepted =
          message.payload?.aborted === true &&
          Array.isArray(message.payload?.runIds) &&
          message.payload.runIds.includes(activeCase.id);
        activeCase.abortedRunIds = message.payload?.runIds || [];
        activeCase.waitRequestId = randomUUID();
        socket.send(
          JSON.stringify({
            type: "req",
            id: activeCase.waitRequestId,
            method: "agent.wait",
            params: { runId: activeCase.id, timeoutMs: 5_000 },
          }),
        );
        return;
      }

      if (
        message.type === "res" &&
        activeCase?.outcome === "abort" &&
        message.id === activeCase.waitRequestId
      ) {
        abortWaitResponse = message;
        if (!message.ok) {
          finish(
            new Error(
              `aborted-run reconciliation rejected: ${JSON.stringify(message.error)}`,
            ),
          );
          return;
        }
        results.push({
          id: activeCase.id,
          input: activeCase.input,
          outcome: activeCase.outcome,
          accepted: activeCase.accepted === true,
          abortAccepted: activeCase.abortAccepted === true,
          abortedRunIds: activeCase.abortedRunIds,
          waitStatus: message.payload?.status,
          waitStopReason: message.payload?.stopReason,
          providerRequestDelta:
            capture.requestCount - activeCase.providerRequestCountBefore,
          configUnchangedDuring:
            digest() === activeCase.configSha256Before,
        });
        sendNextCase();
        return;
      }

      if (
        message.type === "res" &&
        activeCase &&
        message.id === activeCase.id
      ) {
        if (activeCase.outcome === "reject") {
          if (message.ok) {
            finish(
              new Error(
                `${activeCase.input} unexpectedly passed the agent control boundary`,
              ),
            );
            return;
          }
          results.push({
            input: activeCase.input,
            outcome: activeCase.outcome,
            rejected: true,
            error: message.error?.message || JSON.stringify(message.error),
            providerRequestDelta:
              capture.requestCount - activeCase.providerRequestCountBefore,
            configUnchangedDuring:
              digest() === activeCase.configSha256Before,
          });
          sendNextCase();
          return;
        }

        if (!message.ok) {
          finish(
            new Error(
              `agent request rejected: ${JSON.stringify(message.error)}`,
            ),
          );
          return;
        }
        if (
          gatewayInvocation.classifyAgentResponse(message, activeCase.id) ===
          "pending"
        ) {
          activeCase.accepted = true;
          if (activeCase.outcome === "abort") {
            sendAbortWhenProviderStarted(activeCase);
          }
          return;
        }
        if (activeCase.outcome === "abort") {
          activeCase.observedTerminalBeforeAbort = message.payload?.status;
          return;
        }
        const text = gatewayInvocation.extractAgentResponseText(message.payload);
        results.push({
          input: activeCase.input,
          outcome: activeCase.outcome,
          accepted: activeCase.accepted === true,
          status: message.payload?.status,
          text,
          finalPromptText: message.payload?.result?.meta?.finalPromptText,
          sessionId:
            message.payload?.result?.meta?.agentMeta?.sessionId || null,
          providerRequestDelta:
            capture.requestCount - activeCase.providerRequestCountBefore,
          configUnchangedDuring:
            digest() === activeCase.configSha256Before,
        });
        sendNextCase();
      }
    } catch (error) {
      finish(error);
    }
  });

  socket.on("error", finish);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
