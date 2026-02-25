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
const { execFile, spawn } = require("child_process");

const PROXY_PORT = 18790;
const PROXY_URL = `http://127.0.0.1:${PROXY_PORT}/v1/chat/completions`;

const MAX_ITERATIONS = 20;
const TOOL_TIMEOUT_MS = 30000;
const HTTP_TIMEOUT_MS = 120000;

const SYSTEM_PROMPT =
  "You are a helpful personal assistant. You are friendly, concise, and knowledgeable. " +
  "You help users with a wide range of tasks. Keep responses concise unless the user asks " +
  "for detail. If you don't know something, say so honestly. You are accessed through " +
  "messaging channels (Telegram, Slack). Keep responses appropriate for chat-style messaging. " +
  "You can also schedule recurring tasks using EventBridge cron. When users ask for reminders, " +
  "scheduled tasks, or recurring actions, use the scheduling tools. Always ask for timezone if not known.";

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
};

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
 * Execute a tool by running the corresponding skill script.
 * Uses execFile with array args (no shell) to prevent injection.
 * write_user_file uses stdin for content to avoid OS ARG_MAX limits.
 */
function executeTool(toolName, args, userId) {
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
async function chat(userMessage, userId) {
  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: userMessage },
  ];

  for (let i = 0; i < MAX_ITERATIONS; i++) {
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
      console.error("[shim] No choices in proxy response");
      return "I received an unexpected response. Please try again.";
    }

    const assistantMessage = choice.message;
    messages.push(assistantMessage);

    // If no tool calls, return the text response
    const toolCalls = assistantMessage.tool_calls;
    if (!toolCalls || toolCalls.length === 0) {
      return assistantMessage.content || "Message processed.";
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
      const result = await executeTool(fnName, fnArgs, userId);

      messages.push({
        role: "tool",
        tool_call_id: toolCall.id,
        content: result,
      });
    }
  }

  console.warn("[shim] Max iterations reached");
  return "I ran into a limit processing your request. Please try rephrasing.";
}

module.exports = { chat, TOOLS, SCRIPT_MAP, TOOL_ENV, buildToolArgs };
