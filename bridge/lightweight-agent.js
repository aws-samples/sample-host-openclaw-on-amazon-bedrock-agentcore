"use strict";

/**
 * Minimal warm-up agent. It intentionally exposes only bounded web retrieval
 * and the same repository-owned workspace semantics as the full runtime.
 */

const dns = require("node:dns").promises;
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const { PROFILE_ADDITIONS } = require("./runtime-policy");

const PROXY_URL = "http://127.0.0.1:18790/v1/chat/completions";
const MAX_ITERATIONS = 10;
const HTTP_TIMEOUT_MS = 120000;
const WEB_FETCH_TIMEOUT_MS = 15000;
const WEB_FETCH_MAX_BYTES = 512 * 1024;
const WEB_FETCH_MAX_TEXT = 50_000;
const WEB_SEARCH_MAX_RESULTS = 8;
const MAX_REDIRECTS = 3;
const MAX_SEARCH_QUERY_LENGTH = 500;

const SYSTEM_PROMPT =
  "You are a concise personal assistant. Your available capabilities are web search, " +
  "web page retrieval, and a persistent workspace with bounded UTF-8 file operations. " +
  "Use only the provided tools. Never claim a capability that is not present. " +
  "Format responses for chat with short paragraphs or bullets, not tables.";

function parameters(properties, required = []) {
  return {
    type: "object",
    properties,
    required,
    additionalProperties: false,
  };
}

const TOOLS = Object.freeze([
  {
    type: "function",
    function: {
      name: "web_search",
      description: "Search the public web for current information.",
      parameters: parameters(
        { query: { type: "string", minLength: 1, maxLength: 500 } },
        ["query"],
      ),
    },
  },
  {
    type: "function",
    function: {
      name: "web_fetch",
      description: "Retrieve one public HTTP or HTTPS page as bounded plain text.",
      parameters: parameters(
        { url: { type: "string", minLength: 1, maxLength: 2048 } },
        ["url"],
      ),
    },
  },
  {
    type: "function",
    function: {
      name: "po_file_list",
      description: "List files in the user's persistent workspace.",
      parameters: parameters({}),
    },
  },
  {
    type: "function",
    function: {
      name: "po_file_read",
      description: "Read one bounded UTF-8 workspace file.",
      parameters: parameters({ path: { type: "string" } }, ["path"]),
    },
  },
  {
    type: "function",
    function: {
      name: "po_file_write",
      description: "Create or replace one bounded UTF-8 workspace file.",
      parameters: parameters(
        { path: { type: "string" }, content: { type: "string" } },
        ["path", "content"],
      ),
    },
  },
  {
    type: "function",
    function: {
      name: "po_file_delete",
      description: "Delete one exact workspace file.",
      parameters: parameters({ path: { type: "string" } }, ["path"]),
    },
  },
]);

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

const BLOCKED_HOSTNAMES = new Set([
  "localhost",
  "metadata.google.internal",
  "metadata.internal",
  "instance-data",
]);

function ipv4FromMappedIpv6(address) {
  const lower = address.toLowerCase();
  if (!lower.startsWith("::ffff:")) return null;
  const suffix = lower.slice("::ffff:".length);
  if (net.isIP(suffix) === 4) return suffix;
  const groups = suffix.split(":");
  if (groups.length !== 2) return null;
  const high = Number.parseInt(groups[0], 16);
  const low = Number.parseInt(groups[1], 16);
  if (
    !Number.isInteger(high) ||
    !Number.isInteger(low) ||
    high < 0 ||
    high > 0xffff ||
    low < 0 ||
    low > 0xffff
  ) {
    return null;
  }
  return [high >> 8, high & 0xff, low >> 8, low & 0xff].join(".");
}

function isBlockedIpv4(address) {
  const octets = address.split(".").map(Number);
  if (octets.length !== 4 || octets.some((part) => !Number.isInteger(part))) {
    return true;
  }
  const [first, second] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    first >= 224
  );
}

function isBlockedIp(address) {
  const normalized = address.replace(/^\[|\]$/g, "").toLowerCase();
  const mapped = ipv4FromMappedIpv6(normalized);
  if (mapped) return isBlockedIpv4(mapped);
  const family = net.isIP(normalized);
  if (family === 4) return isBlockedIpv4(normalized);
  if (family === 6) {
    return (
      normalized === "::" ||
      normalized === "::1" ||
      normalized.startsWith("fc") ||
      normalized.startsWith("fd") ||
      /^fe[89ab]/.test(normalized)
    );
  }
  return false;
}

function validateUrlSafety(urlString) {
  if (!urlString || typeof urlString !== "string") return "URL is required";
  let parsed;
  try {
    parsed = new URL(urlString);
  } catch {
    return "Invalid URL format";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `Unsupported protocol: ${parsed.protocol}`;
  }
  const hostname = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (BLOCKED_HOSTNAMES.has(hostname)) return `Blocked hostname: ${hostname}`;
  if (net.isIP(hostname) && isBlockedIp(hostname)) {
    return `Blocked IP address: ${hostname}`;
  }
  return null;
}

async function resolvePublicAddress(hostname) {
  const normalized = hostname.replace(/^\[|\]$/g, "");
  if (net.isIP(normalized)) {
    if (isBlockedIp(normalized)) throw new Error(`Blocked IP address: ${normalized}`);
    return { address: normalized, family: net.isIP(normalized) };
  }
  const addresses = await dns.lookup(normalized, { all: true, verbatim: true });
  if (addresses.length === 0) throw new Error("DNS resolution returned no addresses");
  for (const candidate of addresses) {
    if (isBlockedIp(candidate.address)) {
      throw new Error(`Blocked resolved IP: ${candidate.address}`);
    }
  }
  return addresses[0];
}

function stripHtml(html) {
  if (!html) return "";
  return html
    .replace(/<script[^>]*>[\s\S]*?<\/\s*script[^>]*>/gi, " ")
    .replace(/<style[^>]*>[\s\S]*?<\/\s*style[^>]*>/gi, " ")
    .replace(/<noscript[^>]*>[\s\S]*?<\/\s*noscript[^>]*>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_match, code) =>
      String.fromCharCode(Number.parseInt(code, 10)),
    )
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ")
    .trim();
}

function parseSearchResults(html) {
  if (!html) return "No results found.";
  const links = [];
  const snippets = [];
  const linkPattern =
    /<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi;
  const snippetPattern =
    /<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/gi;
  let match;
  while ((match = linkPattern.exec(html)) !== null) {
    let url = match[1];
    const redirect = url.match(/[?&]uddg=([^&]+)/);
    if (redirect) {
      try {
        url = decodeURIComponent(redirect[1]);
      } catch {
        // Preserve the original bounded URL if the redirect parameter is bad.
      }
    }
    links.push({ title: stripHtml(match[2]), url });
  }
  while ((match = snippetPattern.exec(html)) !== null) {
    snippets.push(stripHtml(match[1]));
  }
  const results = links.slice(0, WEB_SEARCH_MAX_RESULTS).map((link, index) => {
    const snippet = snippets[index] ? `\n   ${snippets[index]}` : "";
    return `${index + 1}. ${link.title}\n   ${link.url}${snippet}`;
  });
  return results.length ? results.join("\n\n") : "No results found.";
}

async function requestPublicText(urlString, depth = 0) {
  if (depth > MAX_REDIRECTS) throw new Error("Too many redirects");
  const safetyError = validateUrlSafety(urlString);
  if (safetyError) throw new Error(safetyError);
  const url = new URL(urlString);
  const resolved = await resolvePublicAddress(url.hostname);
  const transport = url.protocol === "https:" ? https : http;

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      callback(value);
    };
    const request = transport.get(
      url,
      {
        timeout: WEB_FETCH_TIMEOUT_MS,
        headers: {
          "User-Agent": "PersonalOperator/0.1",
          Accept: "text/html,application/xhtml+xml,text/plain,*/*",
        },
        lookup: (_hostname, _options, callback) =>
          callback(null, resolved.address, resolved.family),
      },
      (response) => {
        if (
          [301, 302, 303, 307, 308].includes(response.statusCode) &&
          response.headers.location
        ) {
          response.resume();
          const redirectUrl = new URL(response.headers.location, url).href;
          requestPublicText(redirectUrl, depth + 1).then(
            (value) => finish(resolve, value),
            (error) => finish(reject, error),
          );
          return;
        }
        if (response.statusCode >= 400) {
          response.resume();
          finish(reject, new Error(`HTTP ${response.statusCode}`));
          return;
        }
        const chunks = [];
        let bytes = 0;
        response.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > WEB_FETCH_MAX_BYTES) {
            response.destroy(new Error("Response exceeds web size limit"));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () =>
          finish(resolve, Buffer.concat(chunks).toString("utf8")),
        );
        response.on("error", (error) => finish(reject, error));
      },
    );
    request.on("error", (error) => finish(reject, error));
    request.on("timeout", () => request.destroy(new Error("Request timed out")));
  });
}

async function executeWebFetch(url) {
  const safetyError = validateUrlSafety(url);
  if (safetyError) return `Error: ${safetyError}`;
  try {
    const html = await requestPublicText(url);
    return stripHtml(html).slice(0, WEB_FETCH_MAX_TEXT) || "(empty page)";
  } catch (error) {
    return `Error: ${error.message}`;
  }
}

async function executeWebSearch(query) {
  if (!query || typeof query !== "string" || !query.trim()) {
    return "Error: Search query is required";
  }
  const bounded = query.trim().slice(0, MAX_SEARCH_QUERY_LENGTH);
  try {
    const html = await requestPublicText(
      `https://html.duckduckgo.com/html/?q=${encodeURIComponent(bounded)}`,
    );
    return parseSearchResults(html);
  } catch (error) {
    return `Error: ${error.message}`;
  }
}

function createToolExecutor({
  workspaceStore,
  webFetch = executeWebFetch,
  webSearch = executeWebSearch,
}) {
  return async (toolName, args = {}) => {
    try {
      switch (toolName) {
        case "web_fetch":
          return webFetch(args.url);
        case "web_search":
          return webSearch(args.query);
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
          return `Error: Unknown tool '${toolName}'`;
      }
    } catch (error) {
      return `Error: ${error.message}`;
    }
  };
}

let defaultToolExecutorPromise;
async function getDefaultToolExecutor() {
  if (!defaultToolExecutorPromise) {
    defaultToolExecutorPromise = import("./plugins/personal-operator/index.js").then(
      (workspacePlugin) =>
        createToolExecutor({ workspaceStore: workspacePlugin.createWorkspaceStore() }),
    );
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

async function chat(userMessage, _unusedActorId, deadlineMs = 0) {
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
      const result = await executeTool(toolCall.function?.name, args);
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
  executeWebFetch,
  executeWebSearch,
  validateUrlSafety,
  stripHtml,
  parseSearchResults,
};
