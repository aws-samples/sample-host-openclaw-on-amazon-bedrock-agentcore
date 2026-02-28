/**
 * Lightweight Agent Shim — Handles messages while OpenClaw starts up.
 *
 * NOT a replacement for OpenClaw — just a warm-up agent that provides
 * immediate responsiveness during the ~2-4 minute OpenClaw startup.
 *
 * Calls the proxy at http://127.0.0.1:18790/v1/chat/completions (OpenAI format).
 * The proxy handles identity context, workspace files, and image support.
 *
 * Supports s3-user-files and eventbridge-cron tools via child_process.
 */

const http = require("http");
const https = require("https");
const { execFile, spawn } = require("child_process");

const PROXY_PORT = 18790;
const PROXY_URL = `http://127.0.0.1:${PROXY_PORT}/v1/chat/completions`;

const MAX_ITERATIONS = 10;
const TOOL_TIMEOUT_MS = 30000;
const HTTP_TIMEOUT_MS = 120000;
const WEB_FETCH_TIMEOUT_MS = 15000;
const WEB_FETCH_MAX_BYTES = 512 * 1024; // 512KB raw HTML limit
const WEB_FETCH_MAX_TEXT = 50000; // 50KB text output limit
const WEB_SEARCH_MAX_RESULTS = 8;

const SYSTEM_PROMPT =
  "You are a helpful personal assistant. You are friendly, concise, and knowledgeable. " +
  "You help users with a wide range of tasks. Keep responses concise unless the user asks " +
  "for detail. If you don't know something, say so honestly. You are accessed through " +
  "messaging channels (Telegram, Slack). Keep responses appropriate for chat-style messaging.\n\n" +
  "Your capabilities:\n" +
  "- **Web search**: Search the web for current information using web_search\n" +
  "- **Web fetch**: Read any web page content using web_fetch\n" +
  "- **File storage**: Read, write, list, and delete files in user's persistent S3 storage\n" +
  "- **Scheduling**: Create, list, update, and delete recurring cron schedules via EventBridge\n\n" +
  "When users ask for reminders, scheduled tasks, or recurring actions, use the scheduling tools. " +
  "Always ask for timezone if not known.\n\n" +
  "After full startup completes (~2-4 minutes), you gain additional capabilities: " +
  "deep research (multi-step analysis), YouTube transcripts, rich Telegram formatting, " +
  "task decomposition with sub-agents, and enhanced web reading via Jina.";

const TOOLS = [
  {
    type: "function",
    function: {
      name: "read_user_file",
      description:
        "Read a file from the user's personal storage. Returns the file contents.",
      parameters: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "The filename to read (e.g. 'notes.md', 'todo.txt')",
          },
        },
        required: ["filename"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "write_user_file",
      description:
        "Write content to a file in the user's personal storage. Creates or overwrites the file.",
      parameters: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "The filename to write (e.g. 'notes.md', 'todo.txt')",
          },
          content: {
            type: "string",
            description: "The content to write to the file",
          },
        },
        required: ["filename", "content"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "list_user_files",
      description:
        "List all files in the user's personal storage. Returns filenames and sizes.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "delete_user_file",
      description: "Delete a file from the user's personal storage.",
      parameters: {
        type: "object",
        properties: {
          filename: {
            type: "string",
            description: "The filename to delete",
          },
        },
        required: ["filename"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "create_schedule",
      description:
        "Create a recurring cron schedule. Use for reminders, scheduled tasks, or recurring actions. " +
        "Cron expressions use EventBridge format: cron(minutes hours day-of-month month day-of-week year). " +
        "Examples: cron(0 9 * * ? *) = daily at 9am, cron(0 17 ? * MON-FRI *) = weekdays at 5pm.",
      parameters: {
        type: "object",
        properties: {
          cron_expression: {
            type: "string",
            description:
              "EventBridge cron or rate expression, e.g. 'cron(0 9 * * ? *)' or 'rate(1 hour)'",
          },
          timezone: {
            type: "string",
            description:
              "IANA timezone, e.g. 'Asia/Shanghai', 'America/New_York', 'UTC'",
          },
          message: {
            type: "string",
            description:
              "The message to deliver at each scheduled time, e.g. 'Time to check your email!'",
          },
          schedule_name: {
            type: "string",
            description: "Optional human-friendly name for the schedule",
          },
        },
        required: ["cron_expression", "timezone", "message"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "list_schedules",
      description: "List all cron schedules for the current user.",
      parameters: {
        type: "object",
        properties: {},
        required: [],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "update_schedule",
      description:
        "Update an existing cron schedule. Can change expression, timezone, message, name, or enable/disable.",
      parameters: {
        type: "object",
        properties: {
          schedule_id: {
            type: "string",
            description: "The schedule ID to update (8-character hex string)",
          },
          expression: {
            type: "string",
            description: "New cron or rate expression",
          },
          timezone: {
            type: "string",
            description: "New IANA timezone",
          },
          message: {
            type: "string",
            description: "New message to deliver",
          },
          name: {
            type: "string",
            description: "New human-friendly name",
          },
          enable: {
            type: "boolean",
            description: "Set to true to enable the schedule",
          },
          disable: {
            type: "boolean",
            description: "Set to true to disable the schedule",
          },
        },
        required: ["schedule_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "delete_schedule",
      description: "Delete a cron schedule permanently.",
      parameters: {
        type: "object",
        properties: {
          schedule_id: {
            type: "string",
            description: "The schedule ID to delete (8-character hex string)",
          },
        },
        required: ["schedule_id"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "web_fetch",
      description:
        "Fetch a web page and return its content as plain text. " +
        "Use this to read articles, documentation, or any web page content.",
      parameters: {
        type: "object",
        properties: {
          url: {
            type: "string",
            description:
              "The full URL to fetch (must start with http:// or https://)",
          },
        },
        required: ["url"],
      },
    },
  },
  {
    type: "function",
    function: {
      name: "web_search",
      description:
        "Search the web using DuckDuckGo. Returns titles, URLs, and snippets " +
        "for the top results. Use this to find current information, news, or answers.",
      parameters: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: "The search query",
          },
        },
        required: ["query"],
      },
    },
  },
];

/**
 * Shared environment for tool child processes.
 */
const TOOL_ENV = {
  PATH: process.env.PATH,
  HOME: process.env.HOME || "/root",
  NODE_PATH: process.env.NODE_PATH || "/app/node_modules",
  NODE_OPTIONS: process.env.NODE_OPTIONS || "",
  AWS_REGION: process.env.AWS_REGION || "us-west-2",
  S3_USER_FILES_BUCKET: process.env.S3_USER_FILES_BUCKET || "",
  EVENTBRIDGE_SCHEDULE_GROUP: process.env.EVENTBRIDGE_SCHEDULE_GROUP || "",
  CRON_LAMBDA_ARN: process.env.CRON_LAMBDA_ARN || "",
  EVENTBRIDGE_ROLE_ARN: process.env.EVENTBRIDGE_ROLE_ARN || "",
  IDENTITY_TABLE_NAME: process.env.IDENTITY_TABLE_NAME || "",
};

const SCRIPT_MAP = {
  read_user_file: "/skills/s3-user-files/read.js",
  write_user_file: "/skills/s3-user-files/write.js",
  list_user_files: "/skills/s3-user-files/list.js",
  delete_user_file: "/skills/s3-user-files/delete.js",
  create_schedule: "/skills/eventbridge-cron/create.js",
  list_schedules: "/skills/eventbridge-cron/list.js",
  update_schedule: "/skills/eventbridge-cron/update.js",
  delete_schedule: "/skills/eventbridge-cron/delete.js",
  web_fetch: null, // In-process tool — no child process script
  web_search: null, // In-process tool — no child process script
};

// --- SSRF prevention: block private/reserved IPs ---

const BLOCKED_IP_PATTERNS = [
  /^127\./, // loopback
  /^10\./, // 10.0.0.0/8
  /^172\.(1[6-9]|2\d|3[01])\./, // 172.16.0.0/12
  /^192\.168\./, // 192.168.0.0/16
  /^169\.254\./, // link-local (AWS IMDS at 169.254.169.254)
  /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./, // RFC 6598 shared address space
  /^0\./, // 0.0.0.0/8
  /^fc00:/i, // IPv6 unique local
  /^fe80:/i, // IPv6 link-local
  /^::1$/, // IPv6 loopback
  /^fd/i, // IPv6 unique local (covers fd00:ec2::254 AWS IMDSv2)
  /^fd00:0*ec2:/i, // AWS IMDSv2 IPv6 — normalized/expanded forms
  // IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) — prevents DNS rebinding via AAAA records
  /^::ffff:127\./i, // IPv4-mapped loopback
  /^::ffff:10\./i, // IPv4-mapped 10.0.0.0/8
  /^::ffff:172\.(1[6-9]|2\d|3[01])\./i, // IPv4-mapped 172.16.0.0/12
  /^::ffff:192\.168\./i, // IPv4-mapped 192.168.0.0/16
  /^::ffff:169\.254\./i, // IPv4-mapped link-local (IMDS)
  /^::ffff:0\./i, // IPv4-mapped 0.0.0.0/8
];

const BLOCKED_HOSTNAMES = new Set([
  "localhost",
  "metadata.google.internal",
  "metadata.internal",
  "instance-data", // GCP metadata alias
]);

/**
 * Check if a URL target is safe (not a private/reserved address).
 * Returns null if safe, or an error message string if blocked.
 */
function validateUrlSafety(urlStr) {
  if (!urlStr || typeof urlStr !== "string") {
    return "URL is required";
  }

  let parsed;
  try {
    parsed = new URL(urlStr);
  } catch {
    return "Invalid URL format";
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `Unsupported protocol: ${parsed.protocol} (only http/https allowed)`;
  }

  const hostname = parsed.hostname.toLowerCase();

  if (BLOCKED_HOSTNAMES.has(hostname)) {
    return `Blocked hostname: ${hostname}`;
  }

  for (const pattern of BLOCKED_IP_PATTERNS) {
    if (pattern.test(hostname)) {
      return `Blocked IP address: ${hostname}`;
    }
  }

  return null; // safe
}

/**
 * Strip HTML tags and return plain text.
 * Removes script/style content, decodes common entities, collapses whitespace.
 */
function stripHtml(html) {
  if (!html) return "";

  let text = html;

  // Remove script, style, and noscript blocks (including content)
  // Closing tags may contain whitespace, attributes, or junk: </script \t\n bar>
  text = text.replace(/<script[^>]*>[\s\S]*?<\/\s*script[^>]*>/gi, " ");
  text = text.replace(/<style[^>]*>[\s\S]*?<\/\s*style[^>]*>/gi, " ");
  text = text.replace(/<noscript[^>]*>[\s\S]*?<\/\s*noscript[^>]*>/gi, " ");

  // Remove HTML comments
  text = text.replace(/<!--[\s\S]*?-->/g, " ");

  // Remove all HTML tags
  text = text.replace(/<[^>]+>/g, " ");

  // Decode common HTML entities (&amp; MUST be last to prevent double-unescaping:
  // e.g. &amp;lt; → &lt; → < if &amp; is decoded first)
  text = text.replace(/&lt;/g, "<");
  text = text.replace(/&gt;/g, ">");
  text = text.replace(/&quot;/g, '"');
  text = text.replace(/&#39;/g, "'");
  text = text.replace(/&nbsp;/g, " ");
  text = text.replace(/&#(\d+);/g, (_, code) =>
    String.fromCharCode(parseInt(code, 10)),
  );
  text = text.replace(/&amp;/g, "&");

  // Collapse whitespace
  text = text.replace(/\s+/g, " ").trim();

  return text;
}

/**
 * Parse DuckDuckGo HTML search results into a readable text format.
 * Extracts titles, URLs, and snippets from result divs.
 */
function parseSearchResults(html) {
  if (!html) return "No results found.";

  const results = [];

  // DuckDuckGo HTML results have class="result" divs with:
  //   <a class="result__a" href="URL">Title</a>
  //   <a class="result__snippet">Snippet text</a>
  const resultPattern =
    /<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi;
  const snippetPattern =
    /<a[^>]*class="result__snippet"[^>]*>([\s\S]*?)<\/a>/gi;

  const links = [];
  let match;
  while ((match = resultPattern.exec(html)) !== null) {
    // DuckDuckGo wraps URLs through /l/?uddg=... redirects.
    // Extract the actual target URL from the uddg parameter.
    let url = match[1];
    try {
      const uddgMatch = url.match(/[?&]uddg=([^&]+)/);
      if (uddgMatch) {
        url = decodeURIComponent(uddgMatch[1]);
      }
    } catch {
      // Keep original URL if decoding fails
    }
    links.push({ url, title: stripHtml(match[2]) });
  }

  const snippets = [];
  while ((match = snippetPattern.exec(html)) !== null) {
    snippets.push(stripHtml(match[1]));
  }

  for (let i = 0; i < Math.min(links.length, WEB_SEARCH_MAX_RESULTS); i++) {
    const entry = `${i + 1}. ${links[i].title}\n   ${links[i].url}`;
    const snippet = snippets[i] ? `\n   ${snippets[i]}` : "";
    results.push(entry + snippet);
  }

  if (results.length === 0) {
    return "No results found.";
  }

  return results.join("\n\n");
}

const MAX_REDIRECTS = 3;
const MAX_SEARCH_QUERY_LENGTH = 500;

/**
 * Resolve hostname and validate resolved IPs against SSRF blocklist.
 * Mitigates DNS rebinding attacks by checking the resolved IP, not just the hostname.
 * Returns null if safe, or an error message if blocked.
 */
async function validateResolvedIps(hostname) {
  const dns = require("dns").promises;
  try {
    const addresses = await dns.lookup(hostname, { all: true });
    for (const addr of addresses) {
      for (const pattern of BLOCKED_IP_PATTERNS) {
        if (pattern.test(addr.address)) {
          return `Blocked resolved IP: ${addr.address} for hostname ${hostname}`;
        }
      }
    }
  } catch (err) {
    return `DNS resolution failed: ${err.message}`;
  }
  return null; // safe
}

/**
 * Fetch a URL and return its content as plain text.
 * Validates URL safety (SSRF prevention including DNS rebinding),
 * enforces timeout, size limits, and redirect depth.
 */
async function executeWebFetch(url, depth = 0) {
  if (depth > MAX_REDIRECTS) {
    return "Error: Too many redirects";
  }

  const validationError = validateUrlSafety(url);
  if (validationError) {
    return `Error: ${validationError}`;
  }

  // DNS rebinding mitigation: resolve and validate IPs before connecting
  const parsed = new URL(url);
  const ipError = await validateResolvedIps(parsed.hostname);
  if (ipError) {
    return `Error: ${ipError}`;
  }

  return new Promise((resolve) => {
    const protocol = url.startsWith("https") ? https : http;
    const req = protocol.get(
      url,
      {
        timeout: WEB_FETCH_TIMEOUT_MS,
        headers: {
          "User-Agent":
            "Mozilla/5.0 (compatible; OpenClawBot/1.0; +https://github.com/aws-samples)",
          Accept: "text/html,application/xhtml+xml,text/plain,*/*",
        },
      },
      (res) => {
        // Follow redirects (with depth counter)
        if (
          [301, 302, 303, 307, 308].includes(res.statusCode) &&
          res.headers.location
        ) {
          const redirectUrl = new URL(res.headers.location, url).href;
          const redirectError = validateUrlSafety(redirectUrl);
          if (redirectError) {
            resolve(`Error: Redirect blocked — ${redirectError}`);
            return;
          }
          res.resume();
          resolve(executeWebFetch(redirectUrl, depth + 1));
          return;
        }

        if (res.statusCode >= 400) {
          res.resume();
          resolve(`Error: HTTP ${res.statusCode}`);
          return;
        }

        let data = "";
        let bytes = 0;
        let resolved = false;
        res.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > WEB_FETCH_MAX_BYTES) {
            // Resolve immediately with collected data before destroying
            if (!resolved) {
              resolved = true;
              const text = stripHtml(data);
              resolve(
                (text.substring(0, WEB_FETCH_MAX_TEXT) || "(empty page)") +
                  "\n\n[Content truncated at size limit]",
              );
            }
            res.destroy();
            return;
          }
          data += chunk;
        });
        res.on("end", () => {
          if (!resolved) {
            resolved = true;
            const text = stripHtml(data);
            resolve(text.substring(0, WEB_FETCH_MAX_TEXT) || "(empty page)");
          }
        });
        res.on("error", (err) => {
          if (!resolved) {
            resolved = true;
            resolve(`Error: ${err.message}`);
          }
        });
      },
    );

    req.on("error", (err) => {
      resolve(`Error: ${err.message}`);
    });
    req.on("timeout", () => {
      req.destroy();
      resolve("Error: Request timed out");
    });
  });
}

/**
 * Search the web using DuckDuckGo's HTML interface.
 * No API key required. Returns formatted results.
 */
async function executeWebSearch(query) {
  if (!query || typeof query !== "string" || !query.trim()) {
    return "Error: Search query is required";
  }

  const trimmedQuery = query.trim().substring(0, MAX_SEARCH_QUERY_LENGTH);
  const searchUrl = `https://html.duckduckgo.com/html/?q=${encodeURIComponent(trimmedQuery)}`;

  return new Promise((resolve) => {
    const req = https.get(
      searchUrl,
      {
        timeout: WEB_FETCH_TIMEOUT_MS,
        headers: {
          "User-Agent":
            "Mozilla/5.0 (compatible; OpenClawBot/1.0; +https://github.com/aws-samples)",
          Accept: "text/html",
        },
      },
      (res) => {
        if (res.statusCode >= 400) {
          res.resume();
          resolve(`Error: Search failed with HTTP ${res.statusCode}`);
          return;
        }

        let data = "";
        let bytes = 0;
        let resolved = false;
        res.on("data", (chunk) => {
          bytes += chunk.length;
          if (bytes > WEB_FETCH_MAX_BYTES) {
            // Resolve with what we have before destroying the stream
            if (!resolved) {
              resolved = true;
              resolve(parseSearchResults(data));
            }
            res.destroy();
            return;
          }
          data += chunk;
        });
        res.on("end", () => {
          if (!resolved) {
            resolved = true;
            resolve(parseSearchResults(data));
          }
        });
        res.on("error", (err) => {
          if (!resolved) {
            resolved = true;
            resolve(`Error: ${err.message}`);
          }
        });
      },
    );

    req.on("error", (err) => {
      resolve(`Error: ${err.message}`);
    });
    req.on("timeout", () => {
      req.destroy();
      resolve("Error: Search request timed out");
    });
  });
}

/**
 * Execute write_user_file via spawn with content piped through stdin.
 * Avoids OS ARG_MAX limits for large content.
 */
function executeWriteTool(args, userId) {
  const content = args.content || "";
  return new Promise((resolve) => {
    const child = spawn(
      "node",
      [SCRIPT_MAP.write_user_file, userId, args.filename || "", "--stdin"],
      {
        env: TOOL_ENV,
        stdio: ["pipe", "pipe", "pipe"],
        timeout: TOOL_TIMEOUT_MS,
      },
    );

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));

    child.on("close", (code) => {
      if (code !== 0) {
        const msg = stderr.trim() || `Process exited with code ${code}`;
        console.log(`[shim] Tool write_user_file error: ${msg}`);
        resolve(`Error: ${msg}`);
        return;
      }
      resolve(stdout || "(no output)");
    });

    child.on("error", (err) => {
      console.log(`[shim] Tool write_user_file spawn error: ${err.message}`);
      resolve(`Error: ${err.message}`);
    });

    child.stdin.write(content);
    child.stdin.end();
  });
}

/**
 * Build the CLI argument array for a tool invocation.
 * Pure function — no side effects, easy to test.
 *
 * @param {string} toolName - The tool name
 * @param {object} args - The tool arguments from the LLM
 * @param {string} userId - The user's actor ID
 * @returns {string[]|null} - Array of CLI args (including script path), or null if unknown tool
 */
function buildToolArgs(toolName, args, userId) {
  const script = SCRIPT_MAP[toolName];
  if (!script) return null;

  switch (toolName) {
    // s3-user-files tools
    case "read_user_file":
      return [script, userId, args.filename || ""];
    case "list_user_files":
      return [script, userId];
    case "delete_user_file":
      return [script, userId, args.filename || ""];
    // eventbridge-cron tools
    case "create_schedule": {
      const result = [
        script,
        userId,
        args.cron_expression || "",
        args.timezone || "",
        args.message || "",
      ];
      // Empty channel/channelTarget placeholders (argv[6]/argv[7]) so schedule_name
      // lands at argv[8+] as expected by create.js. Empty strings fall back to extractChannelInfo(userId).
      if (args.schedule_name) result.push("", "", args.schedule_name);
      return result;
    }
    case "list_schedules":
      return [script, userId];
    case "update_schedule": {
      const result = [script, userId, args.schedule_id || ""];
      if (args.expression) result.push("--expression", args.expression);
      if (args.timezone) result.push("--timezone", args.timezone);
      if (args.message) result.push("--message", args.message);
      if (args.name) result.push("--name", args.name);
      if (args.enable === true && args.disable !== true)
        result.push("--enable");
      if (args.disable === true && args.enable !== true)
        result.push("--disable");
      return result;
    }
    case "delete_schedule":
      return [script, userId, args.schedule_id || ""];
    default:
      return null;
  }
}

/**
 * Execute a tool by running the corresponding skill script or in-process handler.
 * Uses execFile with array args (no shell) to prevent injection.
 * write_user_file uses stdin for content to avoid OS ARG_MAX limits.
 * web_fetch and web_search are handled in-process (no child process).
 */
function executeTool(toolName, args, userId) {
  // In-process web tools (no child process spawn)
  if (toolName === "web_fetch") {
    return executeWebFetch(args.url);
  }
  if (toolName === "web_search") {
    return executeWebSearch(args.query);
  }

  // write_user_file uses spawn+stdin for content delivery
  if (toolName === "write_user_file") {
    return executeWriteTool(args, userId);
  }

  const scriptArgs = buildToolArgs(toolName, args, userId);
  if (!scriptArgs) {
    return Promise.resolve(`Error: Unknown tool '${toolName}'`);
  }

  return new Promise((resolve) => {
    execFile(
      "node",
      scriptArgs,
      {
        timeout: TOOL_TIMEOUT_MS,
        maxBuffer: 1024 * 1024,
        env: TOOL_ENV,
      },
      (error, stdout, stderr) => {
        if (error) {
          const msg = stderr?.trim() || error.message;
          console.log(`[shim] Tool ${toolName} error: ${msg}`);
          resolve(`Error: ${msg}`);
          return;
        }
        resolve(stdout || "(no output)");
      },
    );
  });
}

/**
 * Make a non-streaming chat completion request to the proxy.
 */
function callProxy(messages) {
  const payload = JSON.stringify({
    model: "bedrock-agentcore",
    messages,
    tools: TOOLS,
    stream: false,
  });

  return new Promise((resolve, reject) => {
    const url = new URL(PROXY_URL);
    const req = http.request(
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
      (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          try {
            const parsed = JSON.parse(body);
            resolve(parsed);
          } catch (e) {
            reject(new Error(`Proxy response parse error: ${e.message}`));
          }
        });
      },
    );

    req.on("error", (err) => reject(err));
    req.on("timeout", () => {
      req.destroy();
      reject(new Error("Proxy request timed out"));
    });
    req.write(payload);
    req.end();
  });
}

/**
 * Run the agent loop: call proxy, execute tool calls if any, repeat until text response.
 *
 * @param {string} userMessage - The user's message text
 * @param {string} userId - The user's actor ID (for tool execution)
 * @returns {Promise<string>} - The assistant's text response
 */
async function chat(userMessage, userId, deadlineMs = 0) {
  // Convert actorId (colon format e.g. "telegram:123") to namespace (underscore format)
  // for tool compatibility — skill scripts expect "telegram_123" format.
  const namespace = userId.replace(/:/g, "_");

  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: userMessage },
  ];

  for (let i = 0; i < MAX_ITERATIONS; i++) {
    if (deadlineMs > 0 && Date.now() > deadlineMs) {
      console.warn(`[shim] Deadline exceeded — stopping at iteration ${i + 1}`);
      break;
    }
    console.log(`[shim] Iteration ${i + 1}/${MAX_ITERATIONS}`);

    let response;
    try {
      response = await callProxy(messages);
    } catch (err) {
      console.error(`[shim] Proxy call failed: ${err.message}`);
      return `I'm having trouble connecting right now. Please try again in a moment.`;
    }

    const choice = response.choices?.[0];
    if (!choice) {
      const errDetail = response.error?.message || JSON.stringify(response).slice(0, 300);
      console.error(`[shim] No choices in proxy response: ${errDetail}`);
      return `I received an unexpected response. Please try again.`;
    }

    const assistantMessage = choice.message;
    messages.push(assistantMessage);

    // If no tool calls, return the text response
    const toolCalls = assistantMessage.tool_calls;
    if (!toolCalls || toolCalls.length === 0) {
      const text =
        assistantMessage.content ||
        "I received your message but couldn't generate a response. Please try again.";
      const footer =
        "\n\n---\n" +
        "_Warm-up mode — after full startup (~2-4 min), additional " +
        "community skills come online: YouTube transcripts, deep research, " +
        "task decomposition with sub-agents, etc._";
      return text + footer;
    }

    // Execute tool calls
    console.log(`[shim] Executing ${toolCalls.length} tool call(s)`);
    for (const toolCall of toolCalls) {
      const fnName = toolCall.function?.name;
      let fnArgs;
      try {
        fnArgs =
          typeof toolCall.function?.arguments === "string"
            ? JSON.parse(toolCall.function.arguments)
            : toolCall.function?.arguments || {};
      } catch {
        fnArgs = {};
      }

      console.log(`[shim] Tool: ${fnName} args=${JSON.stringify(fnArgs)}`);
      const result = await executeTool(fnName, fnArgs, namespace);

      messages.push({
        role: "tool",
        tool_call_id: toolCall.id,
        content: result,
      });
    }
  }

  console.warn("[shim] Max iterations reached");
  const fallbackFooter =
    "\n\n---\n" +
    "_Warm-up mode — after full startup (~2-4 min), additional " +
    "community skills come online: YouTube transcripts, deep research, " +
    "task decomposition with sub-agents, etc._";
  return "I ran into a limit processing your request. Please try rephrasing." + fallbackFooter;
}

module.exports = {
  chat,
  TOOLS,
  SCRIPT_MAP,
  TOOL_ENV,
  buildToolArgs,
  stripHtml,
  parseSearchResults,
  executeWebFetch,
  executeWebSearch,
};
