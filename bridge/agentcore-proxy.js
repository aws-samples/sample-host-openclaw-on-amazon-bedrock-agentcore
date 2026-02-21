/**
 * Bedrock Proxy Adapter
 *
 * Translates OpenAI-compatible chat completion requests from OpenClaw
 * into Bedrock Converse API calls. Runs inside the OpenClaw container
 * hosted on AgentCore Runtime.
 */

const http = require("http");
const crypto = require("crypto");

const PORT = 18790;
const AWS_REGION = process.env.AWS_REGION || "us-west-2";
const MODEL_ID =
  process.env.BEDROCK_MODEL_ID || "us.anthropic.claude-sonnet-4-6";

// Cognito identity configuration
const COGNITO_USER_POOL_ID = process.env.COGNITO_USER_POOL_ID || "";
const COGNITO_CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
const COGNITO_PASSWORD_SECRET = process.env.COGNITO_PASSWORD_SECRET || "";

const SYSTEM_PROMPT =
  "You are a helpful personal assistant powered by OpenClaw. You are friendly, " +
  "concise, and knowledgeable. You help users with a wide range of tasks including " +
  "answering questions, providing information, having conversations, and assisting " +
  "with daily tasks. Keep responses concise unless the user asks for detail. " +
  "If you don't know something, say so honestly. You are accessed through messaging " +
  "channels (WhatsApp, Telegram, Discord, Slack, or a web UI). Keep your responses " +
  "appropriate for chat-style messaging.";

// Retry configuration
const MAX_RETRIES = 3;
const BASE_DELAY_MS = 500;

// Session tracking (in-memory, per container instance)
const sessionMap = new Map();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Extract session metadata from request headers and body.
 * Returns { sessionId, actorId, channel }.
 */
function extractSessionMetadata(parsed, headers) {
  // 1. Check custom headers (future: OpenClaw might set these)
  let actorId = headers["x-openclaw-actor-id"] || "";
  let channel = headers["x-openclaw-channel"] || "unknown";
  let sessionId = headers["x-openclaw-session-id"] || "";

  // 2. Check OpenAI 'user' field (OpenClaw may populate this)
  if (!actorId && parsed.user) {
    actorId = parsed.user;
  }

  // 3. Fallback to default
  if (!actorId) {
    actorId = "default-user";
  }

  // 4. Generate stable session ID (AgentCore requires min 33 chars)
  if (!sessionId) {
    const key = `${actorId}:${channel}`;
    if (!sessionMap.has(key)) {
      const ts = Date.now().toString(36);
      const rand = crypto.randomBytes(12).toString("hex");
      sessionMap.set(key, `ses-${ts}-${rand}-${crypto.createHash("md5").update(key).digest("hex").slice(0, 8)}`);
    }
    sessionId = sessionMap.get(key);
  }

  return { sessionId, actorId, channel };
}

/**
 * Derive a deterministic password for a Cognito user from the HMAC secret.
 */
function derivePassword(actorId) {
  return crypto.createHmac("sha256", COGNITO_PASSWORD_SECRET)
    .update(actorId)
    .digest("base64url")
    .slice(0, 32);
}

// JWT token cache: actorId → { token, expiresAt }
const tokenCache = new Map();

// Lazily initialized Cognito client
let _cognitoClient = null;
function getCognitoClient() {
  if (!_cognitoClient) {
    const { CognitoIdentityProviderClient } = require("@aws-sdk/client-cognito-identity-provider");
    _cognitoClient = new CognitoIdentityProviderClient({ region: AWS_REGION });
  }
  return _cognitoClient;
}

/**
 * Ensure a Cognito user exists for the given actorId. Creates one if not found.
 */
async function ensureCognitoUser(actorId) {
  const { AdminGetUserCommand, AdminCreateUserCommand, AdminSetUserPasswordCommand } =
    require("@aws-sdk/client-cognito-identity-provider");
  const client = getCognitoClient();

  try {
    await client.send(new AdminGetUserCommand({
      UserPoolId: COGNITO_USER_POOL_ID,
      Username: actorId,
    }));
  } catch (err) {
    if (err.name === "UserNotFoundException") {
      const password = derivePassword(actorId);
      await client.send(new AdminCreateUserCommand({
        UserPoolId: COGNITO_USER_POOL_ID,
        Username: actorId,
        MessageAction: "SUPPRESS",
        TemporaryPassword: password,
      }));
      await client.send(new AdminSetUserPasswordCommand({
        UserPoolId: COGNITO_USER_POOL_ID,
        Username: actorId,
        Password: password,
        Permanent: true,
      }));
      console.log(`[proxy] Cognito user provisioned: ${actorId}`);
    } else {
      throw err;
    }
  }
}

/**
 * Get a JWT token for the given actorId (cached, auto-refreshes).
 * Returns null if Cognito is not configured.
 */
async function getCognitoToken(actorId) {
  if (!COGNITO_USER_POOL_ID || !COGNITO_CLIENT_ID || !COGNITO_PASSWORD_SECRET) {
    return null;
  }

  // Check cache
  const cached = tokenCache.get(actorId);
  if (cached && cached.expiresAt > Date.now()) {
    return cached.token;
  }

  await ensureCognitoUser(actorId);

  const { AdminInitiateAuthCommand } = require("@aws-sdk/client-cognito-identity-provider");
  const client = getCognitoClient();

  const response = await client.send(new AdminInitiateAuthCommand({
    UserPoolId: COGNITO_USER_POOL_ID,
    ClientId: COGNITO_CLIENT_ID,
    AuthFlow: "ADMIN_USER_PASSWORD_AUTH",
    AuthParameters: {
      USERNAME: actorId,
      PASSWORD: derivePassword(actorId),
    },
  }));

  const token = response.AuthenticationResult.IdToken;
  const expiresIn = response.AuthenticationResult.ExpiresIn || 3600;
  tokenCache.set(actorId, {
    token,
    expiresAt: Date.now() + (expiresIn - 60) * 1000,
  });

  console.log(`[proxy] Cognito token acquired for ${actorId} (expires in ${expiresIn}s)`);
  return token;
}

/**
 * Convert OpenAI messages to Bedrock Converse format.
 */
function convertMessages(messages) {
  const bedrockMessages = [];
  for (const msg of messages) {
    if (msg.role === "system") continue;
    if (msg.role === "user" || msg.role === "assistant") {
      bedrockMessages.push({
        role: msg.role,
        content: [{ text: typeof msg.content === "string" ? msg.content : JSON.stringify(msg.content) }],
      });
    }
  }

  const systemMessages = messages.filter((m) => m.role === "system");
  const systemText = systemMessages.length > 0
    ? systemMessages.map((m) => m.content).join("\n")
    : SYSTEM_PROMPT;

  return { bedrockMessages, systemText };
}

/**
 * Call Bedrock Converse API (non-streaming).
 */
async function invokeBedrock(messages) {
  const { BedrockRuntimeClient, ConverseCommand } = require(
    "@aws-sdk/client-bedrock-runtime"
  );
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages);

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        console.log(`[proxy] Retry attempt ${attempt + 1}/${MAX_RETRIES} after ${delay}ms`);
        await sleep(delay);
      }

      const response = await client.send(
        new ConverseCommand({
          modelId: MODEL_ID,
          messages: bedrockMessages,
          system: [{ text: systemText }],
          inferenceConfig: { maxTokens: 2048, temperature: 0.7 },
        })
      );

      const outputMessage = response.output?.message;
      if (outputMessage && outputMessage.content) {
        const textParts = outputMessage.content.filter((c) => c.text).map((c) => c.text);
        return {
          text: textParts.join("") || "I received your message but have no response.",
          usage: response.usage || {},
        };
      }
      return { text: "I received your message but have no response.", usage: {} };
    } catch (err) {
      lastError = err;
      console.error(`[proxy] Bedrock invocation attempt ${attempt + 1} failed:`, err.message);
      if (err.$metadata && err.$metadata.httpStatusCode < 500) break;
    }
  }
  throw lastError || new Error("Bedrock invocation failed after retries");
}

/**
 * Call Bedrock ConverseStream API and write SSE chunks to the HTTP response.
 */
async function invokeBedrockStreaming(messages, res, model) {
  const { BedrockRuntimeClient, ConverseStreamCommand } = require(
    "@aws-sdk/client-bedrock-runtime"
  );
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages);

  const chatId = `chatcmpl-${Date.now()}`;
  const created = Math.floor(Date.now() / 1000);
  let inputTokens = 0;
  let outputTokens = 0;

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        console.log(`[proxy] Stream retry ${attempt + 1}/${MAX_RETRIES} after ${delay}ms`);
        await sleep(delay);
      }

      const response = await client.send(
        new ConverseStreamCommand({
          modelId: MODEL_ID,
          messages: bedrockMessages,
          system: [{ text: systemText }],
          inferenceConfig: { maxTokens: 2048, temperature: 0.7 },
        })
      );

      // Write SSE headers
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
      });

      for await (const event of response.stream) {
        if (event.contentBlockDelta?.delta?.text) {
          const chunk = {
            id: chatId,
            object: "chat.completion.chunk",
            created,
            model: model || MODEL_ID,
            choices: [{
              index: 0,
              delta: { content: event.contentBlockDelta.delta.text },
              finish_reason: null,
            }],
          };
          res.write(`data: ${JSON.stringify(chunk)}\n\n`);
        }

        if (event.metadata?.usage) {
          inputTokens = event.metadata.usage.inputTokens || 0;
          outputTokens = event.metadata.usage.outputTokens || 0;
        }
      }

      // Send final chunk with finish_reason
      const finalChunk = {
        id: chatId,
        object: "chat.completion.chunk",
        created,
        model: model || MODEL_ID,
        choices: [{
          index: 0,
          delta: {},
          finish_reason: "stop",
        }],
      };
      res.write(`data: ${JSON.stringify(finalChunk)}\n\n`);
      res.write("data: [DONE]\n\n");
      res.end();

      console.log(`[proxy] Stream complete: ${inputTokens}in/${outputTokens}out tokens`);
      return;
    } catch (err) {
      lastError = err;
      console.error(`[proxy] Stream attempt ${attempt + 1} failed:`, err.message);
      if (err.$metadata && err.$metadata.httpStatusCode < 500) break;
    }
  }

  // If all retries failed and headers not yet sent
  if (!res.headersSent) {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      error: { message: "Bedrock streaming failed: " + lastError.message, type: "proxy_error" },
    }));
  } else {
    res.end();
  }
}

/**
 * Format a response as an OpenAI-compatible chat completion response.
 */
function formatChatResponse(result, model) {
  return {
    id: `chatcmpl-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: model || MODEL_ID,
    choices: [
      {
        index: 0,
        message: {
          role: "assistant",
          content: result.text,
        },
        finish_reason: "stop",
      },
    ],
    usage: {
      prompt_tokens: result.usage.inputTokens || 0,
      completion_tokens: result.usage.outputTokens || 0,
      total_tokens: (result.usage.inputTokens || 0) + (result.usage.outputTokens || 0),
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
    res.end(JSON.stringify({
      status: "ok",
      model: MODEL_ID,
      cognito: COGNITO_USER_POOL_ID ? "configured" : "disabled",
    }));
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
          `[proxy] Incoming request: ${messages.length} messages, model=${parsed.model || MODEL_ID}, stream=${stream}`
        );

        // Extract identity for all modes (used for logging + Cognito)
        const { sessionId, actorId, channel } = extractSessionMetadata(parsed, req.headers);

        // Acquire Cognito JWT (non-blocking failure — logs warning and continues)
        let cognitoToken = null;
        try {
          cognitoToken = await getCognitoToken(actorId);
        } catch (err) {
          console.warn(`[proxy] Cognito token acquisition failed for ${actorId}:`, err.message);
        }

        // --- Direct Bedrock path ---
        if (stream) {
          await invokeBedrockStreaming(messages, res, parsed.model);
        } else {
          const result = await invokeBedrock(messages);
          const response = formatChatResponse(result, parsed.model);
          console.log(
            `[proxy] Response: ${result.usage.inputTokens || "?"}in/${result.usage.outputTokens || "?"}out tokens`
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
            })
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
      })
    );
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: "Not found" }));
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[proxy] Bedrock proxy adapter listening on http://0.0.0.0:${PORT} (model: ${MODEL_ID})`
  );
  console.log(
    `[proxy] Cognito identity: ${COGNITO_USER_POOL_ID ? `pool=${COGNITO_USER_POOL_ID} client=${COGNITO_CLIENT_ID}` : "disabled"}`
  );
});
