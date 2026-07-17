/**
 * Bedrock Proxy Adapter
 *
 * Translates OpenAI-compatible chat completion requests from OpenClaw
 * into Bedrock Converse API calls. Runs inside the OpenClaw container
 * hosted on AgentCore Runtime.
 */

const http = require("http");
const crypto = require("crypto");
const fs = require("node:fs");
const { canonicalNamespace } = require("./session-binding");
const { requireExactRegion } = require("./scoped-credentials");

const PORT = 18790;
const AWS_REGION = requireExactRegion(process.env);
const MODEL_ID =
  process.env.BEDROCK_MODEL_ID || "minimax.minimax-m2.1";


// Subagent model routing — distinct model name lets proxy detect subagent requests
const SUBAGENT_MODEL_NAME = process.env.SUBAGENT_MODEL_NAME || "bedrock-agentcore-subagent";
const SUBAGENT_BEDROCK_MODEL_ID = process.env.SUBAGENT_BEDROCK_MODEL_ID || MODEL_ID;

// Bedrock Guardrails — content filtering (undefined = disabled)
const GUARDRAIL_ID = process.env.BEDROCK_GUARDRAIL_ID || "";
const GUARDRAIL_VERSION = process.env.BEDROCK_GUARDRAIL_VERSION || "DRAFT";
const guardrailConfig = GUARDRAIL_ID
  ? { guardrailIdentifier: GUARDRAIL_ID, guardrailVersion: GUARDRAIL_VERSION }
  : undefined;
if (guardrailConfig) {
  console.log(`[proxy] Bedrock Guardrails enabled: ${GUARDRAIL_ID} v${GUARDRAIL_VERSION}`);
}

// Diagnostic state — exposed via /health for observability (container stdout not in CloudWatch)
let lastIdentityDiag = null;
let chatRequestCount = 0;
let subagentRequestCount = 0;

const SYSTEM_PROMPT =
  "You are a helpful personal assistant powered by OpenClaw. You are friendly, " +
  "concise, and knowledgeable. You help users with a wide range of tasks including " +
  "answering questions, providing information, having conversations, and assisting " +
  "with daily tasks. Keep responses concise unless the user asks for detail. " +
  "If you don't know something, say so honestly. You are accessed through messaging " +
  "channels (WhatsApp, Telegram, Discord, Slack, or a web UI). Keep your responses " +
  "appropriate for chat-style messaging. Do not use markdown tables in responses — use bullet lists or plain paragraphs, as they render better in chat interfaces like Telegram and Slack.";

// Retry configuration
const MAX_RETRIES = 3;
const BASE_DELAY_MS = 500;

// Bedrock request timeout — prevents indefinite hangs if a model stops responding.
// 90s per attempt is generous (most Converse calls complete in <30s); with 3 retries
// the worst case is ~4.5 min, well under the lightweight agent's 120s HTTP timeout
// for non-streaming and the Router Lambda's 600s timeout for streaming.
const BEDROCK_REQUEST_TIMEOUT_MS = 90_000;

/**
 * Cross-region inference profiles (global.* / us.*) require AWS's global
 * routing layer and CANNOT be served by the regional Bedrock Runtime VPC
 * endpoint. When a regional VPC endpoint intercepts the DNS query (via
 * Private DNS), it silently hangs cross-region requests after 90 s.
 *
 * Fix: for cross-region profiles, override the endpoint to the public
 * Bedrock Runtime URL so the SDK bypasses the VPC endpoint and routes
 * through the NAT gateway → public AWS backbone.
 * Regional model IDs (e.g. anthropic.claude-3-haiku-20240307-v1:0) are
 * unaffected and continue to use the VPC endpoint via Private DNS.
 */
function isCrossRegionProfile(modelId) {
  return /^(global|us|eu|ap)\.[a-z]/.test(modelId);
}

function bedrockClientOptions(modelId) {
  const opts = { requestTimeout: BEDROCK_REQUEST_TIMEOUT_MS };
  if (isCrossRegionProfile(modelId)) {
    // Force public endpoint — bypasses VPC endpoint Private DNS intercept.
    opts.endpoint = `https://bedrock-runtime.${AWS_REGION}.amazonaws.com`;
  }
  return opts;
}

function createBedrockClient(modelId, { BedrockRuntimeClient } = {}) {
  const Constructor =
    BedrockRuntimeClient ||
    require("@aws-sdk/client-bedrock-runtime").BedrockRuntimeClient;
  return new Constructor({
    region: AWS_REGION,
    requestHandler: bedrockClientOptions(modelId),
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function resolveRuntimeIdentity(env = process.env) {
  const internalUserId = canonicalNamespace(env.INTERNAL_USER_ID);
  const namespace = canonicalNamespace(env.PERSONAL_OPERATOR_WORKSPACE_PREFIX);
  if (namespace !== internalUserId) {
    throw new Error("Proxy namespace must exactly equal internal user identity");
  }
  return Object.freeze({ internalUserId, namespace });
}

const RUNTIME_IDENTITY = resolveRuntimeIdentity(process.env);

// Lazily initialized S3 client
let _s3Client = null;
function createScopedCredentialFileProvider(
  credentialsFile,
  { readFile = fs.readFileSync, now = () => Date.now() } = {},
) {
  if (typeof credentialsFile !== "string" || credentialsFile.length === 0) {
    throw new Error("An explicit scoped credential file is required for S3");
  }
  return async () => {
    let document;
    try {
      document = JSON.parse(readFile(credentialsFile, "utf8"));
    } catch (error) {
      throw new Error(`Scoped credential file cannot be read: ${error.message}`);
    }
    const expiration = new Date(document?.Expiration);
    if (
      document?.Version !== 1 ||
      typeof document.AccessKeyId !== "string" ||
      document.AccessKeyId.length === 0 ||
      typeof document.SecretAccessKey !== "string" ||
      document.SecretAccessKey.length === 0 ||
      typeof document.SessionToken !== "string" ||
      document.SessionToken.length === 0 ||
      !Number.isFinite(expiration.getTime()) ||
      expiration.getTime() <= now()
    ) {
      throw new Error("Scoped credentials are incomplete or expired");
    }
    return {
      accessKeyId: document.AccessKeyId,
      secretAccessKey: document.SecretAccessKey,
      sessionToken: document.SessionToken,
      expiration,
    };
  };
}

function createScopedS3Client({
  env = process.env,
  S3ClientConstructor,
} = {}) {
  const region = requireExactRegion(env);
  const credentials = createScopedCredentialFileProvider(
    env.PERSONAL_OPERATOR_SCOPED_CREDENTIALS_FILE,
  );
  const Constructor =
    S3ClientConstructor || require("@aws-sdk/client-s3").S3Client;
  return new Constructor({ region, credentials });
}

function getS3Client() {
  if (!_s3Client) {
    _s3Client = createScopedS3Client();
  }
  return _s3Client;
}

// ---------------------------------------------------------------------------
// Image support — extract markers, fetch from S3, build multimodal content
// ---------------------------------------------------------------------------

const ALLOWED_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
]);
const CONTENT_TYPE_TO_BEDROCK_FORMAT = {
  "image/jpeg": "jpeg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
};
const IMAGE_MARKER_REGEX = /\n?\n?\[OPENCLAW_IMAGES:(\[.*?\])\]\s*$/;
const MAX_IMAGE_BYTES = 3_750_000; // 3.75 MB — Bedrock limit
const IMAGE_KEY_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u;

/**
 * Extract image references from text that contains the [OPENCLAW_IMAGES:...] marker.
 * Returns { cleanText, images } where images is an array of { s3Key, contentType }.
 */
function extractImageReferences(text) {
  if (typeof text !== "string") return { cleanText: text, images: [] };

  const match = text.match(IMAGE_MARKER_REGEX);
  if (!match) return { cleanText: text, images: [] };

  const cleanText = text.slice(0, match.index).trimEnd();
  try {
    const images = JSON.parse(match[1]);
    if (!Array.isArray(images)) return { cleanText, images: [] };
    // Validate each image entry
    const validImages = images.filter(
      (img) =>
        img.s3Key &&
        img.contentType &&
        ALLOWED_IMAGE_TYPES.has(img.contentType),
    );
    return { cleanText, images: validImages };
  } catch {
    return { cleanText, images: [] };
  }
}

const VALID_BEDROCK_FORMATS = new Set(["jpeg", "png", "gif", "webp"]);

function isValidImageKey(s3Key, expectedNamespace) {
  let namespace;
  try {
    namespace = canonicalNamespace(expectedNamespace);
  } catch {
    return false;
  }
  if (
    typeof s3Key !== "string" ||
    s3Key.length === 0 ||
    s3Key.includes("..") ||
    s3Key.includes("\\") ||
    IMAGE_KEY_CONTROL_CHARACTERS.test(s3Key)
  ) {
    return false;
  }
  const expectedPrefix = `${namespace}/_uploads/`;
  return s3Key.startsWith(expectedPrefix) && s3Key.length > expectedPrefix.length;
}

/**
 * Fetch an image from S3 by key. Returns { bytes: Buffer, format: string } or null.
 * Validates that the key belongs to the expected user namespace and contains no
 * path traversal sequences.
 */
async function fetchImageFromS3(s3Key, expectedNamespace) {
  if (!isValidImageKey(s3Key, expectedNamespace)) {
    console.warn(
      `[proxy] Rejected S3 image key outside the bound namespace`,
    );
    return null;
  }

  const bucket = process.env.S3_USER_FILES_BUCKET;
  if (!bucket) {
    console.warn(
      "[proxy] S3_USER_FILES_BUCKET not configured — cannot fetch image",
    );
    return null;
  }

  try {
    const { GetObjectCommand } = require("@aws-sdk/client-s3");
    const s3 = getS3Client();
    const resp = await s3.send(
      new GetObjectCommand({ Bucket: bucket, Key: s3Key }),
    );
    const chunks = [];
    for await (const chunk of resp.Body) {
      chunks.push(chunk);
    }
    const bytes = Buffer.concat(chunks);

    if (bytes.length > MAX_IMAGE_BYTES) {
      console.warn(
        `[proxy] S3 image too large: ${bytes.length} bytes (key=${s3Key})`,
      );
      return null;
    }

    // Determine Bedrock format from content type or extension (validated)
    const contentType = resp.ContentType || "";
    const rawFormat =
      CONTENT_TYPE_TO_BEDROCK_FORMAT[contentType] ||
      (s3Key.includes(".") ? s3Key.split(".").pop().toLowerCase() : null);
    const format =
      rawFormat && VALID_BEDROCK_FORMATS.has(rawFormat) ? rawFormat : "jpeg";

    console.log(
      `[proxy] Fetched image from S3: ${s3Key} (${bytes.length} bytes, format=${format})`,
    );
    return { bytes, format };
  } catch (err) {
    console.error(
      `[proxy] Failed to fetch image from S3: ${s3Key} — ${err.message}`,
    );
    return null;
  }
}

/**
 * Convert OpenAI tool definitions to Bedrock toolConfig format.
 * OpenAI: { type: "function", function: { name, description, parameters } }
 * Bedrock: { toolSpec: { name, description, inputSchema: { json: ... } } }
 */
function convertTools(openaiTools) {
  if (!openaiTools || !Array.isArray(openaiTools) || openaiTools.length === 0)
    return undefined;

  const tools = openaiTools
    .filter((t) => t.type === "function" && t.function)
    .map((t) => ({
      toolSpec: {
        name: t.function.name,
        description: t.function.description || "",
        inputSchema: { json: t.function.parameters || {} },
      },
    }));

  return tools.length > 0 ? { tools } : undefined;
}

/**
 * Convert OpenAI messages to Bedrock Converse format.
 * Handles user, assistant (with tool_calls), and tool (tool results) roles.
 */
function convertMessages(messages) {
  const bedrockMessages = [];
  for (const msg of messages) {
    if (msg.role === "system") continue;

    if (msg.role === "user") {
      // Handle multimodal content (array with text + image_bedrock parts)
      if (Array.isArray(msg.content)) {
        const bedrockContent = [];
        for (const part of msg.content) {
          if (part.type === "text" && part.text) {
            bedrockContent.push({ text: part.text });
          } else if (part.type === "image_bedrock" && part.image) {
            bedrockContent.push({ image: part.image });
          }
        }
        if (bedrockContent.length > 0) {
          bedrockMessages.push({ role: "user", content: bedrockContent });
        }
      } else {
        bedrockMessages.push({
          role: "user",
          content: [
            {
              text:
                typeof msg.content === "string"
                  ? msg.content
                  : JSON.stringify(msg.content),
            },
          ],
        });
      }
    } else if (msg.role === "assistant") {
      const content = [];
      // Add text content if present
      if (msg.content) {
        content.push({
          text:
            typeof msg.content === "string"
              ? msg.content
              : JSON.stringify(msg.content),
        });
      }
      // Convert OpenAI tool_calls to Bedrock toolUse blocks
      if (msg.tool_calls && Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) {
          if (tc.type === "function") {
            let args = {};
            try {
              args =
                typeof tc.function.arguments === "string"
                  ? JSON.parse(tc.function.arguments)
                  : tc.function.arguments || {};
            } catch {
              args = {};
            }
            content.push({
              toolUse: {
                toolUseId: tc.id || `tool-${Date.now()}`,
                name: tc.function.name,
                input: args,
              },
            });
          }
        }
      }
      if (content.length > 0) {
        bedrockMessages.push({ role: "assistant", content });
      }
    } else if (msg.role === "tool") {
      // OpenAI tool result → Bedrock toolResult in a user message
      // Bedrock expects toolResult inside a user-role message
      const toolResultContent = {
        toolResult: {
          toolUseId: msg.tool_call_id || "unknown",
          content: [
            {
              text:
                typeof msg.content === "string"
                  ? msg.content
                  : JSON.stringify(msg.content),
            },
          ],
        },
      };
      // If the previous message is already a user message with toolResult, append
      const prev = bedrockMessages[bedrockMessages.length - 1];
      if (
        prev &&
        prev.role === "user" &&
        prev.content.some((c) => c.toolResult)
      ) {
        prev.content.push(toolResultContent);
      } else {
        bedrockMessages.push({
          role: "user",
          content: [toolResultContent],
        });
      }
    }
  }

  const systemMessages = messages.filter((m) => m.role === "system");
  const systemText =
    systemMessages.length > 0
      ? systemMessages.map((m) => m.content).join("\n")
      : SYSTEM_PROMPT;

  return { bedrockMessages, systemText };
}

/**
 * Determine if the incoming request is from a subagent.
 * Returns true when the requested model name matches the distinct subagent model name.
 */
function isSubagentRequest(parsed) {
  if (!parsed || !parsed.model) return false;
  const requested = parsed.model;
  // Match both "bedrock-agentcore-subagent" and "agentcore/bedrock-agentcore-subagent"
  return requested === SUBAGENT_MODEL_NAME ||
    requested.endsWith(`/${SUBAGENT_MODEL_NAME}`);
}

/**
 * Resolve the Bedrock model ID based on the requested model name.
 * Subagent requests → SUBAGENT_BEDROCK_MODEL_ID, everything else → MODEL_ID.
 */
function resolveModelId(requestedModel) {
  if (!requestedModel) return MODEL_ID;
  if (requestedModel === SUBAGENT_MODEL_NAME ||
      requestedModel.endsWith(`/${SUBAGENT_MODEL_NAME}`)) {
    return SUBAGENT_BEDROCK_MODEL_ID;
  }
  return MODEL_ID;
}

/**
 * Call Bedrock Converse API (non-streaming).
 * Accepts optional systemTextOverride and toolConfig for tool use.
 */
async function invokeBedrock(messages, systemTextOverride, toolConfig, requestedModel) {
  const { ConverseCommand } = require("@aws-sdk/client-bedrock-runtime");
  const modelId = resolveModelId(requestedModel);
  const client = createBedrockClient(modelId);
  const { bedrockMessages, systemText } = convertMessages(messages);
  const finalSystemText = systemTextOverride || systemText;

  const params = {
    modelId,
    messages: bedrockMessages,
    system: [{ text: finalSystemText }],
    inferenceConfig: { maxTokens: 16384, temperature: 0.7 },
    ...(guardrailConfig && { guardrailConfig }),
  };
  if (toolConfig) params.toolConfig = toolConfig;

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        console.log(
          `[proxy] Retry attempt ${attempt + 1}/${MAX_RETRIES} after ${delay}ms`,
        );
        await sleep(delay);
      }

      const response = await client.send(new ConverseCommand(params));

      // Log guardrail trace if present
      if (response?.trace?.guardrail) {
        console.debug("[guardrail] trace:", JSON.stringify(response.trace.guardrail));
      }
      // Handle guardrail intervention
      if (response?.stopReason === "guardrail_intervened") {
        console.warn("[guardrail] intervention on non-streaming response");
      }

      const outputMessage = response.output?.message;
      if (outputMessage && outputMessage.content) {
        const textParts = outputMessage.content
          .filter((c) => c.text)
          .map((c) => c.text);
        // Check for tool use in response
        const toolUseParts = outputMessage.content.filter((c) => c.toolUse);
        const toolCalls = toolUseParts.map((c) => ({
          id: c.toolUse.toolUseId,
          type: "function",
          function: {
            name: c.toolUse.name,
            arguments: JSON.stringify(c.toolUse.input || {}),
          },
        }));

        return {
          text:
            textParts.join("") ||
            (toolCalls.length > 0
              ? ""
              : "I received your message but have no response."),
          toolCalls: toolCalls.length > 0 ? toolCalls : undefined,
          finishReason: toolCalls.length > 0 ? "tool_calls" : "stop",
          usage: response.usage || {},
        };
      }
      return {
        text: "I received your message but have no response.",
        usage: {},
        finishReason: "stop",
      };
    } catch (err) {
      lastError = err;
      console.error(
        `[proxy] Bedrock invocation attempt ${attempt + 1} failed:`,
        err.message,
      );
      if (err.$metadata && err.$metadata.httpStatusCode < 500) break;
    }
  }
  throw lastError || new Error("Bedrock invocation failed after retries");
}

/**
 * Call Bedrock ConverseStream API and write SSE chunks to the HTTP response.
 * Accepts optional systemTextOverride and toolConfig for tool use.
 * Returns the full accumulated response text.
 */
async function invokeBedrockStreaming(
  messages,
  res,
  model,
  systemTextOverride,
  toolConfig,
) {
  const { ConverseStreamCommand } = require("@aws-sdk/client-bedrock-runtime");
  const modelId = resolveModelId(model);
  const client = createBedrockClient(modelId);
  const { bedrockMessages, systemText } = convertMessages(messages);
  const finalSystemText = systemTextOverride || systemText;

  const params = {
    modelId,
    messages: bedrockMessages,
    system: [{ text: finalSystemText }],
    inferenceConfig: { maxTokens: 16384, temperature: 0.7 },
    ...(guardrailConfig && { guardrailConfig }),
  };
  if (toolConfig) params.toolConfig = toolConfig;

  const chatId = `chatcmpl-${Date.now()}`;
  const created = Math.floor(Date.now() / 1000);
  let inputTokens = 0;
  let outputTokens = 0;
  let fullResponseText = "";

  // Track tool use blocks during streaming
  const toolCalls = [];
  let currentToolUse = null;
  let currentToolInput = "";
  let currentToolBlockIndex = -1;

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        console.log(
          `[proxy] Stream retry ${attempt + 1}/${MAX_RETRIES} after ${delay}ms`,
        );
        await sleep(delay);
        fullResponseText = "";
        toolCalls.length = 0;
        currentToolUse = null;
        currentToolInput = "";
        currentToolBlockIndex = -1;
      }

      const response = await client.send(new ConverseStreamCommand(params));

      // Write SSE headers
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });

      for await (const event of response.stream) {
        // Text content
        if (event.contentBlockDelta?.delta?.text) {
          const textDelta = event.contentBlockDelta.delta.text;
          fullResponseText += textDelta;
          const chunk = {
            id: chatId,
            object: "chat.completion.chunk",
            created,
            model: model || MODEL_ID,
            choices: [
              {
                index: 0,
                delta: { content: textDelta },
                finish_reason: null,
              },
            ],
          };
          res.write(`data: ${JSON.stringify(chunk)}\n\n`);
        }

        // Tool use start
        if (event.contentBlockStart?.start?.toolUse) {
          const tu = event.contentBlockStart.start.toolUse;
          currentToolUse = { id: tu.toolUseId, name: tu.name };
          currentToolInput = "";
          currentToolBlockIndex = event.contentBlockStart.contentBlockIndex ?? -1;
        }

        // Tool use input delta
        if (event.contentBlockDelta?.delta?.toolUse) {
          const inputChunk = event.contentBlockDelta.delta.toolUse.input || "";
          currentToolInput += inputChunk;
        }

        // Content block stop — finalize tool use only when the stopped block matches the tool block
        const stopBlockIndex = event.contentBlockStop?.contentBlockIndex ?? -1;
        if (event.contentBlockStop && currentToolUse && stopBlockIndex === currentToolBlockIndex) {
          let parsedInput = {};
          try {
            parsedInput = JSON.parse(currentToolInput);
          } catch {}
          const toolCallIndex = toolCalls.length;
          toolCalls.push({
            id: currentToolUse.id,
            type: "function",
            function: {
              name: currentToolUse.name,
              arguments: JSON.stringify(parsedInput),
            },
          });
          // Send tool call chunk in OpenAI streaming format
          const toolChunk = {
            id: chatId,
            object: "chat.completion.chunk",
            created,
            model: model || MODEL_ID,
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index: toolCallIndex,
                      id: currentToolUse.id,
                      type: "function",
                      function: {
                        name: currentToolUse.name,
                        arguments: JSON.stringify(parsedInput),
                      },
                    },
                  ],
                },
                finish_reason: null,
              },
            ],
          };
          res.write(`data: ${JSON.stringify(toolChunk)}\n\n`);
          currentToolUse = null;
          currentToolInput = "";
          currentToolBlockIndex = -1;
        }

        if (event.metadata?.usage) {
          inputTokens = event.metadata.usage.inputTokens || 0;
          outputTokens = event.metadata.usage.outputTokens || 0;
        }

        // Log guardrail trace/intervention from streaming events
        if (event.metadata?.trace?.guardrail) {
          console.debug("[guardrail] stream trace:", JSON.stringify(event.metadata.trace.guardrail));
        }
        if (event.messageStop?.stopReason === "guardrail_intervened") {
          console.warn("[guardrail] intervention on streaming response");
        }
      }

      // Send final chunk with appropriate finish_reason
      const finishReason = toolCalls.length > 0 ? "tool_calls" : "stop";
      const finalChunk = {
        id: chatId,
        object: "chat.completion.chunk",
        created,
        model: model || MODEL_ID,
        choices: [
          {
            index: 0,
            delta: {},
            finish_reason: finishReason,
          },
        ],
      };
      res.write(`data: ${JSON.stringify(finalChunk)}\n\n`);
      res.write("data: [DONE]\n\n");
      res.end();

      console.log(
        `[proxy] Stream complete: ${inputTokens}in/${outputTokens}out tokens` +
          (toolCalls.length > 0 ? `, ${toolCalls.length} tool call(s)` : ""),
      );
      return fullResponseText;
    } catch (err) {
      lastError = err;
      console.error(
        `[proxy] Stream attempt ${attempt + 1} failed:`,
        err.message,
      );
      if (err.$metadata && err.$metadata.httpStatusCode < 500) break;
    }
  }

  // If all retries failed and headers not yet sent
  if (!res.headersSent) {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: {
          message: "Bedrock streaming failed: " + lastError.message,
          type: "proxy_error",
        },
      }),
    );
  } else {
    res.end();
  }
  return "";
}

/**
 * Format a response as an OpenAI-compatible chat completion response.
 * Includes tool_calls if present in the result.
 */
function formatChatResponse(result, model) {
  const message = {
    role: "assistant",
    content: result.text || null,
  };
  if (result.toolCalls) {
    message.tool_calls = result.toolCalls;
  }

  return {
    id: `chatcmpl-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: model || MODEL_ID,
    choices: [
      {
        index: 0,
        message,
        finish_reason: result.finishReason || "stop",
      },
    ],
    usage: {
      prompt_tokens: result.usage.inputTokens || 0,
      completion_tokens: result.usage.outputTokens || 0,
      total_tokens:
        (result.usage.inputTokens || 0) + (result.usage.outputTokens || 0),
    },
  };
}

/**
 * HTTP request handler.
 */
const server = http.createServer(async (req, res) => {
  // Health check
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "ok",
        model: MODEL_ID,
        subagent_model: SUBAGENT_BEDROCK_MODEL_ID,
        subagent_model_name: SUBAGENT_MODEL_NAME,
        total_requests: chatRequestCount,
        subagent_requests: subagentRequestCount,
      }),
    );
    return;
  }

  // Chat completions endpoint
  if (req.method === "POST" && req.url === "/v1/chat/completions") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      try {
        const parsed = JSON.parse(body);
        const messages = parsed.messages || [];
        const stream = parsed.stream === true;

        console.log(
          `[proxy] Incoming request: ${messages.length} messages, model=${parsed.model || MODEL_ID}, stream=${stream}`,
        );

        chatRequestCount++;

        // Detect and count subagent requests
        const isSubagent = isSubagentRequest(parsed);
        if (isSubagent) {
          subagentRequestCount++;
          console.log(
            `[proxy] Subagent request #${subagentRequestCount}: model=${parsed.model}, messages=${messages.length}`,
          );
        }

        // Store identity diagnostic (visible via /health since container stdout not in CloudWatch)
        lastIdentityDiag = {
          internalUserId: RUNTIME_IDENTITY.internalUserId,
          namespace: RUNTIME_IDENTITY.namespace,
          idSource: "server-environment",
          msgCount: messages.length,
          toolCount: parsed.tools ? parsed.tools.length : 0,
          timestamp: new Date().toISOString(),
        };

        // --- Convert OpenAI tools to Bedrock toolConfig ---
        const toolConfig = convertTools(parsed.tools);
        if (toolConfig) {
          console.log(
            `[proxy] Tools: ${toolConfig.tools.length} tool(s) forwarded to Bedrock`,
          );
        }

        // --- Preprocess images: extract markers from last user message ---
        let processedMessages = messages;
        const namespace = RUNTIME_IDENTITY.namespace;
        const lastUserIdx = messages.reduce(
          (acc, m, i) => (m.role === "user" ? i : acc),
          -1,
        );
        if (lastUserIdx >= 0) {
          const lastUser = messages[lastUserIdx];
          const textContent =
            typeof lastUser.content === "string"
              ? lastUser.content
              : Array.isArray(lastUser.content)
                ? lastUser.content
                    .filter((p) => p.type === "text")
                    .map((p) => p.text)
                    .join("")
                : "";
          const { cleanText, images } = extractImageReferences(textContent);
          if (images.length > 0) {
            console.log(
              `[proxy] Found ${images.length} image reference(s) in last user message`,
            );
            const contentParts = [];
            if (cleanText) {
              contentParts.push({ type: "text", text: cleanText });
            }
            for (const img of images) {
              const fetched = await fetchImageFromS3(img.s3Key, namespace);
              if (fetched) {
                contentParts.push({
                  type: "image_bedrock",
                  image: {
                    format: fetched.format,
                    source: { bytes: fetched.bytes },
                  },
                });
              } else {
                console.warn(
                  `[proxy] Skipping unfetchable image: ${img.s3Key}`,
                );
              }
            }
            // Fall back to original message if all images failed and no text
            if (contentParts.length > 0) {
              // Build new messages array (immutable — don't mutate original)
              processedMessages = [
                ...messages.slice(0, lastUserIdx),
                { ...lastUser, content: contentParts },
                ...messages.slice(lastUserIdx + 1),
              ];
            } else {
              console.warn(
                "[proxy] All images failed to fetch and no text — using original message",
              );
            }
          }
        }

        // Preserve the trusted caller-provided system text exactly. User
        // workspace files and legacy memory templates are never promoted into
        // the system channel.
        const systemMessages = messages.filter((m) => m.role === "system");
        const baseSystemText =
          systemMessages.length > 0
            ? systemMessages.map((m) => m.content).join("\n")
            : SYSTEM_PROMPT;
        const systemTextOverride = baseSystemText;

        // --- Direct Bedrock path ---
        if (stream) {
          await invokeBedrockStreaming(
            processedMessages,
            res,
            parsed.model,
            systemTextOverride,
            toolConfig,
          );
        } else {
          const result = await invokeBedrock(
            processedMessages,
            systemTextOverride,
            toolConfig,
            parsed.model,
          );
          const response = formatChatResponse(result, parsed.model);
          console.log(
            `[proxy] Response: ${result.usage.inputTokens || "?"}in/${result.usage.outputTokens || "?"}out tokens`,
          );
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(response));
        }
      } catch (err) {
        console.error("[proxy] Request failed:", err.message);
        if (!res.headersSent) {
          res.writeHead(502, { "Content-Type": "application/json" });
          res.end(
            JSON.stringify({
              error: {
                message: "Invocation failed: " + err.message,
                type: "proxy_error",
              },
            }),
          );
        }
      }
    });
    return;
  }

  // Models list (required by some OpenAI-compatible clients)
  if (req.method === "GET" && req.url === "/v1/models") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        object: "list",
        data: [
          {
            id: "bedrock-agentcore",
            object: "model",
            owned_by: "aws",
          },
        ],
      }),
    );
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

function startProxyServer() {
  console.log(`[proxy] AWS_REGION=${AWS_REGION} MODEL_ID=${MODEL_ID}`);
  return server.listen(PORT, "127.0.0.1", () => {
    console.log(
      `[proxy] Bedrock proxy adapter listening on http://127.0.0.1:${PORT} (model: ${MODEL_ID})`,
    );
  });
}

if (require.main === module) startProxyServer();

module.exports = {
  resolveRuntimeIdentity,
  createBedrockClient,
  createScopedCredentialFileProvider,
  createScopedS3Client,
  extractImageReferences,
  isValidImageKey,
  fetchImageFromS3,
  startProxyServer,
};
