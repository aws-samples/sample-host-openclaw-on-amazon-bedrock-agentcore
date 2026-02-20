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

// AgentCore Memory configuration
const AGENTCORE_MEMORY_ID = process.env.AGENTCORE_MEMORY_ID || "";

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
 * Parse actorId and channel from OpenClaw's message envelope.
 *
 * OpenClaw wraps every inbound message in an envelope like:
 *   DMs:    "[Telegram DM from telegram:6087229962]\nHello"
 *   Groups: "[Telegram Group from telegram:group:-100123]\nAlice (id:6087229962): Hello"
 *
 * For DMs the `from` field IS the per-user identity.
 * For groups the `from` field is the group/channel ID; the individual sender
 * appears as "(id:<user_id>)" in the message body — we extract the last one
 * (most recent speaker) and combine it with the channel prefix.
 *
 * Returns { actorId, channel } or null if no envelope found.
 */
function parseEnvelopeIdentity(messages) {
  // Find the last user-role message (most recent inbound)
  const userMessages = (messages || []).filter((m) => m.role === "user");
  if (userMessages.length === 0) return null;

  const lastMsg = userMessages[userMessages.length - 1];
  const text = typeof lastMsg.content === "string"
    ? lastMsg.content
    : JSON.stringify(lastMsg.content);

  // Match the envelope header: [Channel Type from channel:id]
  const envelopeRe = /\[(\w+)\s+\w+\s+from\s+((?:telegram|discord|slack):\S+)\]/i;
  const envMatch = text.match(envelopeRe);
  if (!envMatch) return null;

  const channelName = envMatch[1].toLowerCase(); // "telegram", "discord", "slack"
  const fromId = envMatch[2];                     // e.g. "telegram:6087229962" or "telegram:group:-100123"

  // Check if this is a group/channel context (contains "group:" or "channel:" in the from ID)
  const isGroup = /:(group|channel):/.test(fromId);

  if (!isGroup) {
    // DM — the from field is the per-user identity
    return { actorId: fromId, channel: channelName };
  }

  // Group/channel — extract the individual sender from "(id:XXXXX)" in the body
  // The envelope body follows the header line. Find all sender ID patterns and use the last one.
  const senderIdRe = /\(id:(\S+?)\)/g;
  let lastSenderId = null;
  let match;
  while ((match = senderIdRe.exec(text)) !== null) {
    lastSenderId = match[1];
  }

  if (lastSenderId) {
    return { actorId: `${channelName}:${lastSenderId}`, channel: channelName };
  }

  // Group message but no individual sender ID found — fall back to group identity
  return { actorId: fromId, channel: channelName };
}

/**
 * Extract session metadata from request headers and body.
 * Returns { sessionId, actorId, channel }.
 *
 * Identity resolution priority:
 *   1. x-openclaw-actor-id header (explicit, custom)
 *   2. OpenAI 'user' field in request body
 *   3. Parse from OpenClaw message envelope text
 *   4. Fallback to "default-user"
 */
function extractSessionMetadata(parsed, headers) {
  // 1. Check custom headers (future: OpenClaw might set these)
  let actorId = headers["x-openclaw-actor-id"] || "";
  let channel = headers["x-openclaw-channel"] || "unknown";
  let sessionId = headers["x-openclaw-session-id"] || "";
  let identitySource = "default";

  // 2. Check OpenAI 'user' field (OpenClaw may populate this)
  if (!actorId && parsed.user) {
    actorId = parsed.user;
    identitySource = "user-field";
  }

  // 3. Parse from message envelope
  if (!actorId) {
    const envelope = parseEnvelopeIdentity(parsed.messages);
    if (envelope) {
      actorId = envelope.actorId;
      channel = envelope.channel;
      identitySource = "envelope";
    }
  }

  // 4. Fallback to default
  if (!actorId) {
    actorId = "default-user";
    identitySource = "default";
  } else if (identitySource === "default") {
    // actorId was set from header
    identitySource = "header";
  }

  // 5. Generate stable session ID (AgentCore requires min 33 chars)
  if (!sessionId) {
    const key = `${actorId}:${channel}`;
    if (!sessionMap.has(key)) {
      const ts = Date.now().toString(36);
      const rand = crypto.randomBytes(12).toString("hex");
      sessionMap.set(key, `ses-${ts}-${rand}-${crypto.createHash("md5").update(key).digest("hex").slice(0, 8)}`);
    }
    sessionId = sessionMap.get(key);
  }

  console.log(`[proxy] Identity resolved: actorId=${actorId} source=${identitySource} channel=${channel} sessionId=${sessionId}`);

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

// Lazily initialized AgentCore Memory client
let _agentCoreClient = null;
function getAgentCoreClient() {
  if (!_agentCoreClient) {
    const { BedrockAgentCoreClient } = require("@aws-sdk/client-bedrock-agentcore");
    _agentCoreClient = new BedrockAgentCoreClient({ region: AWS_REGION });
  }
  return _agentCoreClient;
}

/**
 * Retrieve memory context for a user from AgentCore Memory.
 * Returns a formatted string to prepend to the system prompt, or empty string.
 */
async function retrieveMemoryContext(actorId, sessionId, latestUserMessage) {
  if (!AGENTCORE_MEMORY_ID) return "";

  const contextParts = [];

  // Retrieve long-term memories (semantic + preferences)
  try {
    const { RetrieveMemoryRecordsCommand } = require("@aws-sdk/client-bedrock-agentcore");
    const response = await getAgentCoreClient().send(
      new RetrieveMemoryRecordsCommand({
        memoryId: AGENTCORE_MEMORY_ID,
        namespace: actorId,
        searchCriteria: {
          searchQuery: latestUserMessage,
        },
        maxResults: 10,
      })
    );

    const records = response.memoryRecordSummaries || [];
    if (records.length > 0) {
      const facts = records
        .map((r) => r.content?.text || "")
        .filter((t) => t);
      if (facts.length > 0) {
        contextParts.push(
          "Relevant memories from previous interactions:\n" +
            facts.map((f) => `- ${f}`).join("\n")
        );
      }
    }
    console.log(`[proxy] Retrieved ${records.length} memory records for ${actorId}`);
  } catch (err) {
    console.warn(`[proxy] Failed to retrieve long-term memories for ${actorId}:`, err.message);
  }

  // List recent short-term events for this session
  try {
    const { ListEventsCommand } = require("@aws-sdk/client-bedrock-agentcore");
    const response = await getAgentCoreClient().send(
      new ListEventsCommand({
        memoryId: AGENTCORE_MEMORY_ID,
        sessionId: sessionId,
        actorId: actorId,
        includePayloads: true,
        maxResults: 20,
      })
    );

    const events = response.events || [];
    if (events.length > 0) {
      const recent = [];
      const lastEvents = events.slice(-10);
      for (const evt of lastEvents) {
        for (const item of evt.payload || []) {
          const conv = item.conversational || {};
          const text = conv.content?.text || "";
          const role = conv.role || "";
          if (text) {
            recent.push(role ? `${role}: ${text}` : text);
          }
        }
      }
      if (recent.length > 0) {
        contextParts.push(
          "Recent conversation context:\n" + recent.join("\n")
        );
      }
    }
  } catch (err) {
    console.warn(`[proxy] Failed to list short-term events for ${actorId}:`, err.message);
  }

  return contextParts.join("\n\n");
}

/**
 * Store a conversation exchange as a memory event (fire-and-forget).
 */
function storeMemoryEvent(actorId, sessionId, userMessage, assistantResponse) {
  if (!AGENTCORE_MEMORY_ID) return;

  const { CreateEventCommand } = require("@aws-sdk/client-bedrock-agentcore");

  getAgentCoreClient()
    .send(
      new CreateEventCommand({
        memoryId: AGENTCORE_MEMORY_ID,
        actorId: actorId,
        sessionId: sessionId,
        eventTimestamp: new Date(),
        payload: [
          {
            conversational: {
              content: { text: userMessage },
              role: "user",
            },
          },
          {
            conversational: {
              content: { text: assistantResponse },
              role: "assistant",
            },
          },
        ],
      })
    )
    .then(() => {
      console.log(`[proxy] Memory event stored for ${actorId}`);
    })
    .catch((err) => {
      console.warn(`[proxy] Failed to store memory event for ${actorId}:`, err.message);
    });
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
 * @param {Array} messages - OpenAI-format messages
 * @param {string} [memoryContext] - Optional AgentCore Memory context to prepend
 */
function convertMessages(messages, memoryContext) {
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
  const baseSystemText = systemMessages.length > 0
    ? systemMessages.map((m) => m.content).join("\n")
    : SYSTEM_PROMPT;

  const now = new Date();
  const today = now.toISOString().split("T")[0];
  const dayOfWeek = now.toLocaleDateString("en-US", { weekday: "long" });
  const dateContext = `[IMPORTANT: Today is ${dayOfWeek}, ${today}. The current year is ${now.getFullYear()}. Do NOT use your training data cutoff as the current date.]`;

  let systemText = `${dateContext}\n\n${baseSystemText}`;
  if (memoryContext) {
    systemText += `\n\n## Relevant Context\n${memoryContext}`;
  }

  return { bedrockMessages, systemText };
}

/**
 * Call Bedrock Converse API (non-streaming).
 */
async function invokeBedrock(messages, memoryContext) {
  const { BedrockRuntimeClient, ConverseCommand } = require(
    "@aws-sdk/client-bedrock-runtime"
  );
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages, memoryContext);

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
 * Returns the full collected response text (for memory storage).
 */
async function invokeBedrockStreaming(messages, res, model, memoryContext) {
  const { BedrockRuntimeClient, ConverseStreamCommand } = require(
    "@aws-sdk/client-bedrock-runtime"
  );
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages, memoryContext);

  const chatId = `chatcmpl-${Date.now()}`;
  const created = Math.floor(Date.now() / 1000);
  let inputTokens = 0;
  let outputTokens = 0;
  const collectedChunks = [];

  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      if (attempt > 0) {
        const delay = BASE_DELAY_MS * Math.pow(2, attempt - 1);
        console.log(`[proxy] Stream retry ${attempt + 1}/${MAX_RETRIES} after ${delay}ms`);
        await sleep(delay);
        collectedChunks.length = 0;
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
          collectedChunks.push(event.contentBlockDelta.delta.text);
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
      return collectedChunks.join("");
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
  return "";
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
      memory: AGENTCORE_MEMORY_ID ? "configured" : "disabled",
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

        // --- Retrieve memory context (non-blocking failure) ---
        const lastUserMsg = [...messages].reverse().find((m) => m.role === "user");
        const lastUserText = lastUserMsg
          ? (typeof lastUserMsg.content === "string" ? lastUserMsg.content : JSON.stringify(lastUserMsg.content))
          : "";

        let memoryContext = "";
        try {
          memoryContext = await retrieveMemoryContext(actorId, sessionId, lastUserText);
        } catch (err) {
          console.warn(`[proxy] Memory retrieval failed for ${actorId}:`, err.message);
        }

        // --- Direct Bedrock path ---
        if (stream) {
          const responseText = await invokeBedrockStreaming(messages, res, parsed.model, memoryContext);
          if (responseText && lastUserText) {
            storeMemoryEvent(actorId, sessionId, lastUserText, responseText);
          }
        } else {
          const result = await invokeBedrock(messages, memoryContext);
          const response = formatChatResponse(result, parsed.model);
          console.log(
            `[proxy] Response: ${result.usage.inputTokens || "?"}in/${result.usage.outputTokens || "?"}out tokens`
          );
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(response));
          if (result.text && lastUserText) {
            storeMemoryEvent(actorId, sessionId, lastUserText, result.text);
          }
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
  console.log(
    `[proxy] AgentCore Memory: ${AGENTCORE_MEMORY_ID ? `id=${AGENTCORE_MEMORY_ID}` : "disabled"}`
  );
});
