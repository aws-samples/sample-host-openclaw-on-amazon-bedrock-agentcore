/**
 * Bedrock Proxy Adapter
 *
 * Translates OpenAI-compatible chat completion requests from OpenClaw
 * into Bedrock Converse API calls. Runs inside the OpenClaw container
 * hosted on AgentCore Runtime.
 */

const http = require("http");
const crypto = require("crypto");
const fs = require("fs");

const PORT = 18790;
const AWS_REGION = process.env.AWS_REGION;
if (!AWS_REGION) {
  console.error("[proxy] FATAL: AWS_REGION environment variable is not set.");
  process.exit(1);
}
const MODEL_ID =
  process.env.BEDROCK_MODEL_ID || "minimax.minimax-m2.1";

// Subagent model routing — distinct model name lets proxy detect subagent requests
const SUBAGENT_MODEL_NAME = process.env.SUBAGENT_MODEL_NAME || "bedrock-agentcore-subagent";
const SUBAGENT_BEDROCK_MODEL_ID = process.env.SUBAGENT_BEDROCK_MODEL_ID || MODEL_ID;

// Cognito identity configuration
const COGNITO_USER_POOL_ID = process.env.COGNITO_USER_POOL_ID || "";
const COGNITO_CLIENT_ID = process.env.COGNITO_CLIENT_ID || "";
const COGNITO_PASSWORD_SECRET = process.env.COGNITO_PASSWORD_SECRET || "";

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
  let actorId = "";
  let channel = "unknown";
  let sessionId = "";
  let idSource = "none";

  // 0. Check shared identity file (updated by contract server on each message,
  //    supports cross-channel identity changes for bound users). Falls back to
  //    env vars if the file doesn't exist or can't be read.
  try {
    const identity = JSON.parse(
      fs.readFileSync("/tmp/current-identity.json", "utf-8"),
    );
    if (identity.actorId) {
      actorId = identity.actorId;
      channel = identity.channel || "unknown";
      idSource = "identity-file";
    }
  } catch (_) {
    // File not yet written or unreadable — fall back to env vars
  }
  if (!actorId && process.env.USER_ID) {
    actorId = process.env.USER_ID;
    channel = process.env.CHANNEL || "unknown";
    idSource = "environment";
  }
  if (actorId && (idSource === "identity-file" || idSource === "environment")) {
    // Generate stable session ID for this user
    const key = `${actorId}:${channel}`;
    if (!sessionMap.has(key)) {
      const ts = Date.now().toString(36);
      const rand = crypto.randomBytes(12).toString("hex");
      sessionMap.set(
        key,
        `ses-${ts}-${rand}-${crypto.createHash("md5").update(key).digest("hex").slice(0, 8)}`,
      );
    }
    sessionId = sessionMap.get(key);
    return { sessionId, actorId, channel, idSource };
  }

  // 1. Check custom headers (future: OpenClaw might set these)
  actorId = headers["x-openclaw-actor-id"] || "";
  channel = headers["x-openclaw-channel"] || "unknown";
  sessionId = headers["x-openclaw-session-id"] || "";
  if (actorId) idSource = "header";

  // 2. Check OpenAI 'user' field (OpenClaw may populate this)
  if (!actorId && parsed.user) {
    actorId = parsed.user;
    idSource = "openai-user-field";
  }

  // Helper: extract text content from string or array (multimodal) format
  function getTextContent(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      const textPart = content.find((p) => p.type === "text" && p.text);
      return textPart ? textPart.text : "";
    }
    return "";
  }

  // 3. Extract from message envelope headers
  // OpenClaw wraps user messages with channel-specific prefixes. Three known formats:
  //
  // Format C (metadata JSON — checked FIRST, highest priority):
  //   Conversation info (untrusted metadata):
  //   ```json
  //   { "message_id": "542", "sender": "123456789" }
  //   ```
  //   NOT anchored — OpenClaw may PREPEND a Slack "System: [...]" line before
  //   the metadata block. Used by ALL channels. Contains the platform user ID
  //   (Telegram numeric, Slack U-prefixed, Discord snowflake) which is the
  //   most stable identifier for namespacing.
  //
  // Format A (fallback — display-name-based):
  //   System: [2026-02-22 11:16:42 UTC] Slack DM from Sen-Outlook: message
  //   Pattern: System: [TIMESTAMP] CHANNEL TYPE from SENDER: message
  //   Uses display names which can change — only used when Format C is absent.
  //
  // Format B (legacy, hypothetical):
  //   [Telegram John Doe id:12345 timestamp] message
  //
  // IMPORTANT: Iterate in REVERSE (most recent message first) to prevent
  // cross-channel identity leakage from older messages in the conversation.
  // A single message can contain both a Slack "System:" prefix and Telegram
  // metadata when OpenClaw merges cross-channel context — Format C's sender
  // ID pattern detection resolves the actual channel correctly.
  if (!actorId && parsed.messages) {
    for (let i = parsed.messages.length - 1; i >= 0; i--) {
      const msg = parsed.messages[i];
      if (msg.role !== "user") continue;
      const text = getTextContent(msg.content);
      if (!text) continue;

      // Format C: Metadata JSON block (all channels)
      // Checked FIRST — contains platform user IDs (stable, unique).
      // NOT anchored — OpenClaw may prepend "System: [...] Slack message edited..."
      const formatC = text.match(
        /Conversation info \(untrusted metadata\):\s*```json\s*(\{[\s\S]*?\})\s*```/,
      );
      if (formatC) {
        try {
          const meta = JSON.parse(formatC[1]);
          if (meta.sender) {
            const senderId = String(meta.sender)
              .replace(/[^a-zA-Z0-9_-]/g, "")
              .slice(0, 64);
            // Determine channel from sender ID format:
            //   meta.channel field (most authoritative if present)
            //   /^[UW][A-Z0-9]{8,}/i → Slack user ID (e.g., U0AGD41CBGS)
            //   /^\d{15,}/ → Discord snowflake ID
            //   /^\d{5,14}/ → Telegram numeric ID
            let channelName = "";
            if (meta.channel) {
              channelName = String(meta.channel)
                .toLowerCase()
                .replace(/[^a-z]/g, "");
            }
            if (!channelName) {
              if (/^[UW][A-Z0-9]{8,}$/i.test(senderId)) {
                channelName = "slack";
              } else if (/^\d{15,}$/.test(senderId)) {
                channelName = "discord";
              } else if (/^\d{5,14}$/.test(senderId)) {
                channelName = "telegram";
              }
            }
            if (!channelName) channelName = "telegram"; // safe fallback for numeric IDs
            actorId = `${channelName}:${senderId}`;
            channel = channelName;
            idSource = "metadata-json";
            break;
          }
        } catch {
          // JSON parse failed, fall through to other formats
        }
      }

      // Format A: "System: [TIMESTAMP] Channel TYPE from SenderName: message"
      // Fallback — uses display names (can change). Only reached when Format C
      // is absent from the message.
      const formatA = text.match(
        /System:\s*\[[^\]]+\]\s*(Slack|Telegram|Discord|WhatsApp)\s+\S+\s+from\s+([^:]+):/i,
      );
      if (formatA) {
        const channelName = formatA[1].toLowerCase();
        const senderName = formatA[2].trim();
        if (senderName) {
          const sanitizedName = senderName
            .toLowerCase()
            .replace(/[^a-z0-9_-]/g, "_")
            .slice(0, 64);
          actorId = `${channelName}:${sanitizedName}`;
          channel = channelName;
          idSource = "envelope-formatA";
        }
        break;
      }

      // Format B: "[Channel ... id:IDENTIFIER ...]"
      const formatB = text.match(
        /^\[(Slack|Telegram|Discord|WhatsApp)\s+[^\]]*?\bid:(\S+)/i,
      );
      if (formatB) {
        const channelName = formatB[1].toLowerCase();
        const rawId = formatB[2]
          .replace(/\)$/, "")
          .replace(/[^a-zA-Z0-9_-]/g, "")
          .slice(0, 64);
        if (rawId) {
          actorId = `${channelName}:${rawId}`;
          channel = channelName;
          idSource = "envelope-formatB";
        }
        break;
      }
    }
  }

  // 4. Extract from message name fields (if OpenClaw sets them)
  if (!actorId && parsed.messages) {
    const userMsg = parsed.messages.find((m) => m.role === "user" && m.name);
    if (userMsg && userMsg.name) {
      actorId = userMsg.name;
      idSource = "message-name";
    }
  }

  // 5. Fallback to default (with warning)
  if (!actorId) {
    actorId = "default-user";
    idSource = "fallback";
    console.warn(
      "[proxy] WARNING: No user identity found in request — using default-user fallback. " +
        "All users share the same S3 namespace.",
    );
  }

  // 5b. Validate actorId format to prevent prompt injection.
  // Only allow channel:alphanumeric patterns or known fallback values.
  const VALID_ACTOR_ID =
    /^(telegram|slack|discord|whatsapp):[A-Za-z0-9_-]{1,64}$/;
  if (actorId !== "default-user" && !VALID_ACTOR_ID.test(actorId)) {
    console.warn(
      `[proxy] WARNING: actorId "${actorId.slice(0, 80)}" failed validation — falling back to default-user.`,
    );
    actorId = "default-user";
    idSource = "fallback-invalid";
  }

  // Diagnostic: log the first user message prefix to verify envelope format
  if (parsed.messages) {
    const firstUserMsg = parsed.messages.find(
      (m) => m.role === "user" && typeof m.content === "string",
    );
    if (firstUserMsg) {
      // Only log the envelope prefix (up to closing bracket), never full message content
      const bracketEnd = firstUserMsg.content.indexOf("]");
      const prefix =
        bracketEnd > 0
          ? firstUserMsg.content.slice(0, bracketEnd + 1)
          : firstUserMsg.content.slice(0, 60);
    }
  }
  console.log(
    `[proxy][identity] actorId=${actorId}, channel=${channel}, source=${idSource}`,
  );

  // 6. Generate stable session ID (AgentCore requires min 33 chars)
  if (!sessionId) {
    const key = `${actorId}:${channel}`;
    if (!sessionMap.has(key)) {
      const ts = Date.now().toString(36);
      const rand = crypto.randomBytes(12).toString("hex");
      sessionMap.set(
        key,
        `ses-${ts}-${rand}-${crypto.createHash("md5").update(key).digest("hex").slice(0, 8)}`,
      );
    }
    sessionId = sessionMap.get(key);
  }

  return { sessionId, actorId, channel, idSource };
}

/**
 * Derive a deterministic password for a Cognito user from the HMAC secret.
 */
function derivePassword(actorId) {
  return crypto
    .createHmac("sha256", COGNITO_PASSWORD_SECRET)
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
    const {
      CognitoIdentityProviderClient,
    } = require("@aws-sdk/client-cognito-identity-provider");
    _cognitoClient = new CognitoIdentityProviderClient({ region: AWS_REGION });
  }
  return _cognitoClient;
}

// Lazily initialized S3 client
let _s3Client = null;
function getS3Client() {
  if (!_s3Client) {
    const { S3Client } = require("@aws-sdk/client-s3");
    _s3Client = new S3Client({ region: AWS_REGION });
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

/**
 * Fetch an image from S3 by key. Returns { bytes: Buffer, format: string } or null.
 * Validates that the key belongs to the expected user namespace and contains no
 * path traversal sequences.
 */
async function fetchImageFromS3(s3Key, expectedNamespace) {
  // Reject path traversal attempts
  if (s3Key.includes("..")) {
    console.warn(`[proxy] Rejected S3 image key with path traversal: ${s3Key}`);
    return null;
  }

  // Validate key belongs to the expected user namespace
  const expectedPrefix = expectedNamespace + "/_uploads/";
  if (!s3Key.startsWith(expectedPrefix)) {
    console.warn(
      `[proxy] Rejected S3 image key outside user namespace: ${s3Key} (expected prefix: ${expectedPrefix})`,
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

// Workspace files pre-loaded into system prompt per user (priority order)
const WORKSPACE_FILES = [
  {
    filename: "AGENTS.md",
    label: "Operating Instructions",
    purpose: "rules, priorities, and behavioral guidelines",
  },
  {
    filename: "SOUL.md",
    label: "Agent Persona",
    purpose: "persona, tone, and communication boundaries",
  },
  {
    filename: "USER.md",
    label: "User Preferences",
    purpose: "user identity and communication preferences",
  },
  {
    filename: "IDENTITY.md",
    label: "Agent Identity",
    purpose: "agent name, vibe, and emoji",
  },
  {
    filename: "TOOLS.md",
    label: "Tools Documentation",
    purpose: "local tools and conventions documentation",
  },
  {
    filename: "MEMORY.md",
    label: "Notes & Memories",
    purpose: "freeform notes and memories",
  },
];
const WORKSPACE_PER_FILE_MAX_CHARS = 4096;
const WORKSPACE_TOTAL_MAX_CHARS = 20000;

// Default templates seeded into a user's S3 namespace on first interaction.
// Minimal starters — users customize via write_user_file.
const WORKSPACE_DEFAULTS = {
  "AGENTS.md":
    "# Operating Instructions\n\n" +
    "- Be helpful, concise, and friendly\n" +
    "- Keep responses appropriate for chat messaging\n" +
    "- If you don't know something, say so honestly\n",
  "SOUL.md":
    "# Agent Persona\n\n" +
    "You are a helpful personal assistant powered by OpenClaw.\n" +
    "Tone: friendly, concise, knowledgeable.\n",
  "USER.md":
    "# User Preferences\n\n" +
    "No preferences set yet. When the user shares their preferences " +
    "(language, tone, format, interests), update this file.\n",
  "IDENTITY.md":
    "# Agent Identity\n\n" +
    "No identity configured yet. When the user gives you a name, " +
    "vibe, or emoji, update this file.\n",
  "TOOLS.md":
    "# Tools\n\n" +
    "## Built-in Tools\n" +
    "- **web_search**: Search the web for current information\n" +
    "- **web_fetch**: Fetch and read web page content\n" +
    "- **exec**: Run shell commands\n" +
    "- **read**: Read local files\n\n" +
    "## Custom Skills\n" +
    "- **s3-user-files**: Read, write, list, and delete files in your personal workspace\n" +
    "- **eventbridge-cron**: Schedule recurring tasks, reminders, and cron jobs\n\n" +
    "## ClawHub Skills\n" +
    "- **duckduckgo-search**: Web search via DuckDuckGo (no API key needed)\n" +
    "- **jina-reader**: Extract web content as clean markdown\n" +
    "- **deep-research-pro**: In-depth multi-step research on complex topics\n" +
    "- **telegram-compose**: Rich HTML formatting for Telegram messages\n" +
    "- **transcript**: YouTube video transcript extraction\n" +
    "- **hackernews**: Browse and search Hacker News\n" +
    "- **news-feed**: RSS-based news aggregation\n" +
    "- **task-decomposer**: Break complex requests into manageable subtasks\n",
  "MEMORY.md":
    "# Notes & Memories\n\n" +
    "No notes yet. When the user asks you to remember something, " +
    "save it here.\n",
};

/**
 * Sanitize workspace file content for safe system prompt injection.
 * Truncates to per-file limit and escapes code fences (both ``` and ~~~)
 * to prevent fence-break injection when content is wrapped in ~~~ fences.
 */
function sanitizeWorkspaceContent(raw) {
  return raw
    .slice(0, WORKSPACE_PER_FILE_MAX_CHARS)
    .replace(/```/g, "\\`\\`\\`")
    .replace(/~~~/g, "\\~\\~\\~");
}

/**
 * Read a user file from S3 (fire-and-forget on error).
 * Returns the file content or empty string.
 */
async function readUserFileFromS3(namespace, filename) {
  const bucket = process.env.S3_USER_FILES_BUCKET;
  if (
    !bucket ||
    !namespace ||
    namespace === "default_user" ||
    namespace === "default-user"
  ) {
    return "";
  }
  try {
    const { GetObjectCommand } = require("@aws-sdk/client-s3");
    const s3 = getS3Client();
    const response = await s3.send(
      new GetObjectCommand({
        Bucket: bucket,
        Key: `${namespace}/${filename}`,
      }),
    );
    const chunks = [];
    for await (const chunk of response.Body) {
      chunks.push(chunk);
    }
    const content = Buffer.concat(chunks).toString("utf-8").trim();
    console.log(
      `[proxy] Read ${filename} for ${namespace}: ${content.length} bytes`,
    );
    return content;
  } catch (err) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404) {
      console.log(`[proxy] No ${filename} for ${namespace} (not created yet)`);
    } else {
      console.warn(
        `[proxy] Failed to read ${filename} for ${namespace}:`,
        err.message,
      );
    }
    return "";
  }
}

/**
 * Write a file to a user's S3 namespace (fire-and-forget on error).
 */
async function writeUserFileToS3(namespace, filename, content) {
  const bucket = process.env.S3_USER_FILES_BUCKET;
  if (
    !bucket ||
    !namespace ||
    namespace === "default_user" ||
    namespace === "default-user"
  ) {
    return;
  }
  try {
    const { PutObjectCommand } = require("@aws-sdk/client-s3");
    const s3 = getS3Client();
    await s3.send(
      new PutObjectCommand({
        Bucket: bucket,
        Key: `${namespace}/${filename}`,
        Body: content,
        ContentType: "text/markdown; charset=utf-8",
      }),
    );
    console.log(
      `[proxy] Seeded ${filename} for ${namespace}: ${content.length} bytes`,
    );
  } catch (err) {
    console.warn(
      `[proxy] Failed to seed ${filename} for ${namespace}:`,
      err.message,
    );
  }
}

/**
 * Seed missing workspace files with default templates.
 * Called fire-and-forget after reading workspace files — does not block
 * the current request. Missing files get defaults so they are available
 * on the next request.
 */
async function ensureWorkspaceFiles(namespace, rawContents) {
  const missing = [];
  for (let i = 0; i < WORKSPACE_FILES.length; i++) {
    const wf = WORKSPACE_FILES[i];
    if (!rawContents[i] && WORKSPACE_DEFAULTS[wf.filename]) {
      missing.push(wf);
    }
  }
  if (missing.length === 0) return;

  console.log(
    `[proxy] Seeding ${missing.length} workspace file(s) for ${namespace}: ${missing.map((w) => w.filename).join(", ")}`,
  );
  await Promise.all(
    missing.map((wf) =>
      writeUserFileToS3(
        namespace,
        wf.filename,
        WORKSPACE_DEFAULTS[wf.filename],
      ),
    ),
  );
}

/**
 * Build user identity context to inject into the system prompt.
 * Pre-loads all workspace files from S3 in parallel, injects per-user
 * isolation rules, and enforces per-file and total size caps.
 */
const VALID_CHANNELS = new Set([
  "telegram",
  "slack",
  "discord",
  "whatsapp",
  "unknown",
]);

async function retrieveMemoryContext(_actorId, _lastUserText) {
  // Stub — memory retrieval not yet implemented.
  return "";
}

async function buildUserIdentityContext(actorId, channel) {
  const safeChannel = VALID_CHANNELS.has(channel) ? channel : "unknown";
  const namespace = actorId.replace(/:/g, "_");

  // Pre-load all workspace files in parallel
  const rawContents = await Promise.all(
    WORKSPACE_FILES.map((wf) => readUserFileFromS3(namespace, wf.filename)),
  );

  // Fire-and-forget: seed any missing workspace files with defaults.
  // Does not block the current request — defaults appear on next request.
  ensureWorkspaceFiles(namespace, rawContents).catch(() => {});

  // Build per-file sections with total cap enforcement
  let totalChars = 0;
  const fileSections = [];
  const skippedFiles = new Set();
  for (let i = 0; i < WORKSPACE_FILES.length; i++) {
    const wf = WORKSPACE_FILES[i];
    const raw = rawContents[i];

    if (raw) {
      const sanitized = sanitizeWorkspaceContent(raw);
      if (totalChars + sanitized.length > WORKSPACE_TOTAL_MAX_CHARS) {
        skippedFiles.add(wf.filename);
        fileSections.push(
          `\n## Workspace: ${wf.label} (${wf.filename})\n` +
            `*Skipped — total workspace size cap reached.* ` +
            `Use \`read_user_file("${namespace}", "${wf.filename}")\` to read on demand.\n`,
        );
        continue;
      }
      totalChars += sanitized.length;
      fileSections.push(
        `\n## Workspace: ${wf.label} (${wf.filename})\n` +
          "Use this data directly — do NOT re-read from S3 unless the user explicitly asks to refresh.\n" +
          "~~~\n" +
          sanitized +
          "\n~~~\n",
      );
    } else {
      fileSections.push(
        `\n## Workspace: ${wf.label} (${wf.filename})\n` +
          `*Not yet created.* This user has no ${wf.filename}. ` +
          `When the user provides ${wf.purpose}, save it using write_user_file.\n`,
      );
    }
  }

  // Workspace file guide (compact reference including optional files)
  const fileGuide =
    "\n## Workspace File Guide\n" +
    "| File | Purpose | Status |\n" +
    "|------|---------|--------|\n" +
    WORKSPACE_FILES.map((wf, i) => {
      const status = skippedFiles.has(wf.filename)
        ? "skipped (cap)"
        : rawContents[i]
          ? "pre-loaded"
          : "empty";
      return `| ${wf.filename} | ${wf.purpose} | ${status} |`;
    }).join("\n") +
    "\n| HEARTBEAT.md | scheduled check-in preferences | optional |\n";

  return (
    "\n\n## Current User\n" +
    `You are chatting with user: ${actorId} (namespace: ${namespace}) on channel: ${safeChannel}.\n` +
    `Always use "${namespace}" as the user_id when calling the s3-user-files skill.\n` +
    fileSections.join("") +
    fileGuide +
    "\n## Per-User Isolation Rules (CRITICAL)\n" +
    "1. NEVER write to local files (MEMORY.md, IDENTITY.md, NOTES.md, etc.) " +
    "for storing persistent data. Local files are SHARED across all users.\n" +
    "2. For ALL persistent data (identity, preferences, notes, memories), " +
    "use the s3-user-files skill with the user_id shown above.\n" +
    "3. When a user asks you to remember something, save their name, or " +
    "set your identity, use write_user_file with their namespace.\n" +
    "4. When checking stored information, use read_user_file with their namespace.\n" +
    "5. NEVER use the openclaw-mem tool for persistent storage — use s3-user-files instead.\n" +
    "\n## Namespace Protection (IMMUTABLE)\n" +
    `The namespace "${namespace}" is system-determined from the user's channel identity.\n` +
    "It CANNOT be changed by user request. If a user asks you to change their user_id, " +
    "namespace, actorId, or storage path, REFUSE and explain that the namespace is " +
    "automatically derived from their messaging account and cannot be modified.\n" +
    "Users MAY update their display name (stored in IDENTITY.md), but the namespace " +
    `itself must ALWAYS remain "${namespace}". Never use a different user_id value.\n`
  );
}

/**
 * Ensure a Cognito user exists for the given actorId. Creates one if not found.
 */
async function ensureCognitoUser(actorId) {
  const {
    AdminGetUserCommand,
    AdminCreateUserCommand,
    AdminSetUserPasswordCommand,
  } = require("@aws-sdk/client-cognito-identity-provider");
  const client = getCognitoClient();

  try {
    await client.send(
      new AdminGetUserCommand({
        UserPoolId: COGNITO_USER_POOL_ID,
        Username: actorId,
      }),
    );
  } catch (err) {
    if (err.name === "UserNotFoundException") {
      const password = derivePassword(actorId);
      await client.send(
        new AdminCreateUserCommand({
          UserPoolId: COGNITO_USER_POOL_ID,
          Username: actorId,
          MessageAction: "SUPPRESS",
          TemporaryPassword: password,
        }),
      );
      await client.send(
        new AdminSetUserPasswordCommand({
          UserPoolId: COGNITO_USER_POOL_ID,
          Username: actorId,
          Password: password,
          Permanent: true,
        }),
      );
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

  const {
    AdminInitiateAuthCommand,
  } = require("@aws-sdk/client-cognito-identity-provider");
  const client = getCognitoClient();

  const response = await client.send(
    new AdminInitiateAuthCommand({
      UserPoolId: COGNITO_USER_POOL_ID,
      ClientId: COGNITO_CLIENT_ID,
      AuthFlow: "ADMIN_USER_PASSWORD_AUTH",
      AuthParameters: {
        USERNAME: actorId,
        PASSWORD: derivePassword(actorId),
      },
    }),
  );

  const token = response.AuthenticationResult.IdToken;
  const expiresIn = response.AuthenticationResult.ExpiresIn || 3600;
  tokenCache.set(actorId, {
    token,
    expiresAt: Date.now() + (expiresIn - 60) * 1000,
  });

  console.log(
    `[proxy] Cognito token acquired for ${actorId} (expires in ${expiresIn}s)`,
  );
  return token;
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
  const {
    BedrockRuntimeClient,
    ConverseCommand,
  } = require("@aws-sdk/client-bedrock-runtime");
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages);
  const finalSystemText = systemTextOverride || systemText;
  const modelId = resolveModelId(requestedModel);

  const params = {
    modelId,
    messages: bedrockMessages,
    system: [{ text: finalSystemText }],
    inferenceConfig: { maxTokens: 2048, temperature: 0.7 },
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
  const {
    BedrockRuntimeClient,
    ConverseStreamCommand,
  } = require("@aws-sdk/client-bedrock-runtime");
  const client = new BedrockRuntimeClient({ region: AWS_REGION });
  const { bedrockMessages, systemText } = convertMessages(messages);
  const finalSystemText = systemTextOverride || systemText;
  const modelId = resolveModelId(model);

  const params = {
    modelId,
    messages: bedrockMessages,
    system: [{ text: finalSystemText }],
    inferenceConfig: { maxTokens: 2048, temperature: 0.7 },
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
        }

        // Tool use input delta
        if (event.contentBlockDelta?.delta?.toolUse) {
          currentToolInput += event.contentBlockDelta.delta.toolUse.input || "";
        }

        // Content block stop — finalize tool use if one was in progress
        if (event.contentBlockStop && currentToolUse) {
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
        }

        if (event.metadata?.usage) {
          inputTokens = event.metadata.usage.inputTokens || 0;
          outputTokens = event.metadata.usage.outputTokens || 0;
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
    // List installed skills in /skills/ for diagnostic visibility
    let installedSkills = [];
    try {
      installedSkills = fs.readdirSync("/skills").filter((d) => {
        try {
          return fs.statSync(`/skills/${d}`).isDirectory();
        } catch {
          return false;
        }
      });
    } catch {}
    // Check if s3-user-files SKILL.md exists
    const s3SkillExists = fs.existsSync("/skills/s3-user-files/SKILL.md");

    res.end(
      JSON.stringify({
        status: "ok",
        model: MODEL_ID,
        subagent_model: SUBAGENT_BEDROCK_MODEL_ID,
        subagent_model_name: SUBAGENT_MODEL_NAME,
        cognito: COGNITO_USER_POOL_ID ? "configured" : "disabled",
        total_requests: chatRequestCount,
        subagent_requests: subagentRequestCount,
        installed_skills: installedSkills,
        s3_skill_exists: s3SkillExists,
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

        // Extract identity for all modes (used for logging + Cognito)
        const { sessionId, actorId, channel, idSource } =
          extractSessionMetadata(parsed, req.headers);
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
          actorId,
          channel,
          idSource,
          msgCount: messages.length,
          toolCount: parsed.tools ? parsed.tools.length : 0,
          timestamp: new Date().toISOString(),
        };

        // Acquire Cognito JWT (non-blocking failure — logs warning and continues)
        let cognitoToken = null;
        try {
          cognitoToken = await getCognitoToken(actorId);
        } catch (err) {
          console.warn(
            `[proxy] Cognito token acquisition failed for ${actorId}:`,
            err.message,
          );
        }

        // --- Convert OpenAI tools to Bedrock toolConfig ---
        const toolConfig = convertTools(parsed.tools);
        if (toolConfig) {
          console.log(
            `[proxy] Tools: ${toolConfig.tools.length} tool(s) forwarded to Bedrock`,
          );
        }

        // --- Preprocess images: extract markers from last user message ---
        let processedMessages = messages;
        const namespace = actorId.replace(/:/g, "_");
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

        // --- Retrieve memory context for this user ---
        const lastUserMsg = [...messages]
          .reverse()
          .find((m) => m.role === "user");
        const lastUserText = lastUserMsg
          ? typeof lastUserMsg.content === "string"
            ? lastUserMsg.content
            : JSON.stringify(lastUserMsg.content)
          : "";
        const memoryContext = await retrieveMemoryContext(
          actorId,
          lastUserText,
        );

        // Build augmented system text with user identity + memory context.
        // Identity is ALWAYS injected; memory context may be empty string.
        const identityContext = await buildUserIdentityContext(
          actorId,
          channel,
        );
        const systemMessages = messages.filter((m) => m.role === "system");
        const baseSystemText =
          systemMessages.length > 0
            ? systemMessages.map((m) => m.content).join("\n")
            : SYSTEM_PROMPT;
        const systemTextOverride = baseSystemText + identityContext + memoryContext;

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

server.listen(PORT, "127.0.0.1", () => {
  console.log(
    `[proxy] Bedrock proxy adapter listening on http://127.0.0.1:${PORT} (model: ${MODEL_ID})`,
  );
  console.log(
    `[proxy] Cognito identity: ${COGNITO_USER_POOL_ID ? `pool=${COGNITO_USER_POOL_ID} client=${COGNITO_CLIENT_ID}` : "disabled"}`,
  );
});
