"use strict";

/**
 * Minimal warm-up agent. It intentionally exposes only the same
 * repository-owned workspace semantics as the full runtime.
 */

const http = require("node:http");
const { PROFILE_ADDITIONS } = require("./runtime-policy");
const {
  CAPABILITY_TOOL_NAMES,
  TOOL_DEFINITIONS,
} = require("./capability-catalog");

// The lightweight runtime holds NO direct network authority. Public URL reading
// is a gateway-mediated capability (po_web_read) reached only through the
// capability relay adapter; there is no in-process fetch, DNS, or TLS path.
// The single retained http require below is solely the loopback model proxy.
const PROXY_URL = "http://127.0.0.1:18790/v1/chat/completions";
const MAX_ITERATIONS = 10;
const HTTP_TIMEOUT_MS = 120000;

const SYSTEM_PROMPT =
  "You are a concise personal assistant. Your available capability is a persistent " +
  "workspace with bounded UTF-8 file operations. " +
  "Use only the provided tools. Never claim a capability that is not present. " +
  "Format responses for chat with short paragraphs or bullets, not tables.";

const TOOLS = Object.freeze(
  PROFILE_ADDITIONS.map((toolName) =>
    Object.freeze({
      type: "function",
      function: Object.freeze({
        name: toolName,
        description: TOOL_DEFINITIONS[toolName].description,
        parameters: TOOL_DEFINITIONS[toolName].parameters,
      }),
    }),
  ),
);

const LIGHTWEIGHT_TOOL_NAMES = PROFILE_ADDITIONS;
const declaredToolNames = TOOLS.map((tool) => tool.function.name);
if (
  declaredToolNames.length !== LIGHTWEIGHT_TOOL_NAMES.length ||
  declaredToolNames.some(
    (toolName, index) => toolName !== LIGHTWEIGHT_TOOL_NAMES[index],
  )
) {
  throw new Error("Lightweight tools have drifted from the frozen runtime policy");
}

function createToolExecutor({
  workspaceStore,
  capabilityAdapters = {},
}) {
  return async (toolName, args = {}, toolUseId = undefined) => {
    try {
      switch (toolName) {
        case "po_file_list": {
          const files = await workspaceStore.list();
          return files.length
            ? files.map((file) => `${file.path} (${file.size} bytes)`).join("\n")
            : "No workspace files.";
        }
        case "po_file_read":
          return workspaceStore.read(args.path);
        case "po_file_write": {
          const result = await workspaceStore.write(args.path, args.content);
          return `Wrote ${result.path} (${result.bytes} bytes).`;
        }
        case "po_file_delete": {
          const result = await workspaceStore.delete(args.path);
          return `Deleted ${result.path}.`;
        }
        default:
          if (CAPABILITY_TOOL_NAMES.includes(toolName)) {
            const adapter =
              capabilityAdapters instanceof Map
                ? capabilityAdapters.get(toolName)
                : Object.hasOwn(capabilityAdapters, toolName)
                  ? capabilityAdapters[toolName]
                  : undefined;
            if (typeof adapter !== "function") {
              return `Error: Capability tool '${toolName}' is disabled`;
            }
            const result = await adapter(toolUseId, args);
            if (!result || typeof result !== "object" || Array.isArray(result)) {
              return `Error: Capability tool '${toolName}' returned an invalid result`;
            }
            return JSON.stringify(result);
          }
          return `Error: Unknown tool '${toolName}'`;
      }
    } catch (error) {
      return `Error: ${error.message}`;
    }
  };
}

let defaultToolExecutorPromise = null;
async function configureWorkspaceRuntime({
  env,
  capabilityAdapters = {},
  loadPlugin = () => import("./plugins/personal-operator/index.js"),
} = {}) {
  if (!env || typeof env !== "object") {
    throw new Error("Explicit scoped workspace environment is required");
  }
  const workspacePlugin = await loadPlugin();
  const workspaceStore = workspacePlugin.createWorkspaceStore({ env });
  const executor = createToolExecutor({ workspaceStore, capabilityAdapters });
  defaultToolExecutorPromise = Promise.resolve(executor);
  return executor;
}

async function getDefaultToolExecutor() {
  if (!defaultToolExecutorPromise) {
    throw new Error("Lightweight workspace is not configured with scoped credentials");
  }
  return defaultToolExecutorPromise;
}

function callProxy(messages) {
  const payload = JSON.stringify({
    model: "bedrock-agentcore",
    messages,
    tools: TOOLS,
    stream: false,
  });
  const url = new URL(PROXY_URL);
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: HTTP_TIMEOUT_MS,
      },
      (response) => {
        let body = "";
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new Error(`Proxy response parse error: ${error.message}`));
          }
        });
      },
    );
    request.on("error", reject);
    request.on("timeout", () => request.destroy(new Error("Proxy request timed out")));
    request.end(payload);
  });
}

async function chat(userMessage, deadlineMs = 0) {
  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: userMessage },
  ];
  const executeTool = await getDefaultToolExecutor();

  for (let iteration = 0; iteration < MAX_ITERATIONS; iteration += 1) {
    if (deadlineMs > 0 && Date.now() > deadlineMs) break;
    let response;
    try {
      response = await callProxy(messages);
    } catch (error) {
      console.error(`[shim] Proxy call failed: ${error.message}`);
      return "I'm having trouble connecting right now. Please try again.";
    }
    const choice = response.choices?.[0];
    if (!choice?.message) return "I received an unexpected response. Please try again.";
    const assistantMessage = choice.message;
    messages.push(assistantMessage);
    const toolCalls = assistantMessage.tool_calls;
    if (!toolCalls?.length) {
      return assistantMessage.content || "I could not generate a response.";
    }
    for (const toolCall of toolCalls) {
      let args = {};
      try {
        args =
          typeof toolCall.function?.arguments === "string"
            ? JSON.parse(toolCall.function.arguments)
            : toolCall.function?.arguments || {};
      } catch {
        args = {};
      }
      const result = await executeTool(
        toolCall.function?.name,
        args,
        toolCall.id,
      );
      messages.push({
        role: "tool",
        tool_call_id: toolCall.id,
        content: result,
      });
    }
  }
  return "I reached the processing limit. Please rephrase the request.";
}

module.exports = {
  SYSTEM_PROMPT,
  TOOLS,
  LIGHTWEIGHT_TOOL_NAMES,
  chat,
  createToolExecutor,
  configureWorkspaceRuntime,
  getDefaultToolExecutor,
};
