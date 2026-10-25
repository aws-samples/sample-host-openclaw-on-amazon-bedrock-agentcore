/**
 * Render OpenClaw's gateway configuration and agent instructions for the bridge.
 *
 * Main and sub-agent requests use distinct model aliases on the local proxy,
 * which selects the actual Bedrock model. The headless gateway has no channels;
 * replies return through the bridge, while skills handle persistent storage,
 * scheduling, and the optional browser. Tool and background-feature settings
 * keep OpenClaw's defaults aligned with that deployment model.
 *
 * Rendering only returns values; the caller supplies the gateway token and
 * decides when to write the configuration and instructions.
 */
const PROXY_PORT = 18790;
const OPENCLAW_PORT = 18789;
const SUBAGENT_MODEL_NAME = "bedrock-agentcore-subagent";

function renderConfig({ gatewayToken } = {}) {
  // Sub-agent model uses a distinct name so the proxy can identify subagent requests.
  // The proxy maps this name → SUBAGENT_BEDROCK_MODEL_ID (or MODEL_ID fallback).
  const subagentModel = `agentcore/${SUBAGENT_MODEL_NAME}`;

  // Keep OpenClaw's generated runtime context in system messages.
  const modelCompat = {
    cacheControlFormat: "anthropic",
    supportsDeveloperRole: false,
    requiresStringContent: false,
  };

  const config = {
    models: {
      providers: {
        agentcore: {
          baseUrl: `http://127.0.0.1:${PROXY_PORT}/v1`,
          apiKey: "local",
          api: "openai-completions",
          models: [
            { id: "bedrock-agentcore", name: "Bedrock AgentCore", compat: modelCompat },
            { id: SUBAGENT_MODEL_NAME, name: "Bedrock AgentCore Subagent", compat: modelCompat },
          ],
        },
      },
    },
    agents: {
      defaults: {
        model: { primary: "agentcore/bedrock-agentcore" },
        models: {
          "agentcore/bedrock-agentcore": { params: { cacheRetention: "short" } },
          [subagentModel]: { params: { cacheRetention: "short" } },
        },
        subagents: {
          model: subagentModel,
          maxConcurrent: 2,
          runTimeoutSeconds: 900,
          archiveAfterMinutes: 60,
        },
        sandbox: {
          mode: "off", // No Docker in AgentCore container; microVMs provide isolation
        },
      },
    },
    tools: {
      profile: "full",
      exec: {
        host: "gateway", // Run on container host — microVM provides isolation, no Docker sandbox
        // "full" = security:full + ask:off. OpenClaw 2.0 made `mode` the canonical
        // exec policy knob; the old `security`/`ask` pair is a legacy input that
        // cannot be combined with `mode`. Container is already isolated and headless.
        mode: "full",
      },
      deny: [
        "write", // Local writes don't persist — use S3 skill instead
        "edit", // Local edits are ephemeral — use S3 skill instead
        "apply_patch", // Code patching not needed for chat assistant
        "read", // Blocks the model's `read` tool — defence-in-depth against reading /proc/<pid>/environ via a convenience path. Note: this does NOT stop `exec` from running `cat /proc/1/environ`; the real control for that is container-level uid / PID-namespace isolation (see bridge/scoped-credentials.js module header). Use the s3-user-files skill for user data.
        "browser", // Deny built-in browser tool — use agentcore-browser skill instead (via exec)
        "canvas", // No UI rendering in headless chat context
        "cron", // EventBridge handles scheduling, not OpenClaw's built-in cron
        "gateway", // Admin tool — not needed for end users
        // --- Tools added by OpenClaw 2.0 (2026.8.x+) that did not exist on 2026.3.8.
        // Denied to keep the pre-2.0 tool surface: they either need a Control UI /
        // human-in-the-loop that this headless bridge cannot provide, or add
        // Bedrock spend for features this deployment does not use.
        "terminal", // Shared operator terminal — Control UI only
        "process", // Background process manager — exec via skills is the supported path
        "plugins", // Agent-driven plugin install/enable — operator concern, not end users
        "ask_user", // Structured human prompt — no UI to answer it here
        "secrets", // Masked credential prompt — API keys are stored via the api-keys skill
        "screen", // Control UI pane layout
        "progress_card", // Control UI progress card
        "nodes", // Paired device inspection — no nodes in this deployment
        "heartbeat_respond", // Ambient heartbeat replies — no channel can receive them
        "image_generate", // Media generation — not routed through the Bedrock proxy
        "music_generate",
        "video_generate",
        "tts",
        // Channel-delivery tools. OpenClaw runs with `channels: {}` and no bot
        // tokens; the bridge returns the final reply text and the router/cron
        // Lambda delivers it. A model call to these (e.g. "send the brief to
        // Telegram") fails with missing_token and the content never reaches
        // the reply, so the user receives nothing.
        "message", // Channel send/actions (Telegram, Slack, ...)
        "conversations_send", // Send to an external conversation address
        "conversations_turn", // Send + wait for an external reply
      ],
      // Note: `exec` is intentionally NOT denied — skills like clawhub-manage
      // need Bash(node:*) to run scripts. The scoped STS credential_process
      // narrows the AWS SDK/CLI access to the user's namespace, but it is
      // NOT a tenant-isolation boundary: the exec shell runs as root in the
      // same PID namespace as this contract server and can therefore read
      // /proc/1/environ or reach the container credential endpoint to
      // recover the full execution-role credentials. Treat the per-user
      // AgentCore microVM as the enforced tenant boundary; cross-user
      // isolation requires container-level uid/PID-namespace controls on
      // top of this env filtering (see bridge/scoped-credentials.js header).
    },
    skills: {
      allowBundled: [],
      load: { extraDirs: ["/skills"] },
      // OpenClaw 2.0 turns on autonomous self-learning (Skill Workshop) by
      // default: extra model calls after runs plus new files under
      // agents/main/agent/workshop-skills that would get synced to S3.
      // Off preserves pre-2.0 behaviour and Bedrock spend.
      workshop: { autonomous: { mode: "off" } },
    },
    // OpenClaw 2.0 default: "keep conversations across idle periods and day
    // boundaries when no reset policy is configured". 2026.3.8 reset daily;
    // keep that so upgraded users see the same conversation lifecycle.
    session: {
      reset: { mode: "daily", atHour: 4 },
    },
    // OpenClaw 2.0 defaults Active Memory cross-conversation recall ON for
    // "personal installs". It adds a retrieval model pass before replies
    // (Bedrock spend) and indexes transcripts. Off preserves pre-2.0 behaviour.
    memory: {
      search: { rememberAcrossConversations: false },
    },
    plugins: {
      entries: {
        // Active Memory plugin (advanced recall path) — off, see memory.search above.
        "active-memory": { enabled: false },
        // Grounded dreaming (background memory consolidation using the model) is
        // on by default in 2.0. OPENCLAW_SKIP_CRON=1 already defers its cron job;
        // disable explicitly so no consolidation runs land on Bedrock.
        "memory-core": { config: { dreaming: { enabled: false } } },
      },
    },
    gateway: {
      mode: "local",
      port: OPENCLAW_PORT,
      trustedProxies: ["127.0.0.1"],
      auth: { mode: "token", token: gatewayToken },
      controlUi: {
        enabled: false,
        // NOTE: `allowInsecureAuth` (removed from the schema) and
        // `dangerouslyDisableDeviceAuth` (retired, ignored) were dropped for
        // OpenClaw 2.0. Leaving them in makes the startup doctor rewrite
        // openclaw.json (+ .bak ring) on every boot. Device-less auth is now
        // handled by the bridge identifying as "gateway-client"/"backend".
        dangerouslyAllowHostHeaderOriginFallback: true,
        allowedOrigins: ["*"],
      },
    },
    channels: {}, // No channels — messages bridged via WebSocket
  };

  return config;
}

function renderAgents({ browserEnabled = false } = {}) {
  return [
        "# Agent Instructions",
        "",
        "You are a helpful AI assistant running in a per-user container on AWS.",
        "You have built-in web tools, file storage, scheduling, and many community skills.",
        "",
        "## Response Formatting",
        "",
        "Format responses for chat messaging apps (Telegram, Slack):",
        "- Your final reply text is delivered to the user automatically (including scheduled tasks); do not try to send messages yourself — put the full answer in your reply",
        "- **No markdown tables** — use bullet lists or plain text paragraphs instead",
        "- Tables do not render in most chat apps; bullets always work",
        "- Keep responses concise and chat-appropriate",
        "",
        "## Built-in Web Tools",
        "",
        "You have built-in **web_search** and **web_fetch** tools:",
        "- **web_search**: Search the web for current information",
        "- **web_fetch**: Fetch and read web page content as markdown",
        "",
        "Use these for real-time information, news, research, and reading web pages.",
        "",
        "## Scheduling & Cron Jobs",
        "",
        "You have the **eventbridge-cron** skill for scheduling tasks. When users ask to set up reminders,",
        "recurring tasks, or cron jobs, use these commands via Bash. Do NOT say cron is disabled.",
        "The built-in cron is replaced by Amazon EventBridge Scheduler (more reliable, persists across sessions).",
        "",
        "Always ask the user for their **timezone** if you don't know it (e.g., Asia/Shanghai, America/New_York).",
        "Omit channel and channel_target to use the current conversation's delivery destination.",
        "Never derive the delivery channel or target from the storage namespace.",
        "",
        "**Commands** (run via Bash):",
        "- Create: `node /skills/eventbridge-cron/create.js <user_id> <cron_expression> <timezone> <message> [channel] [channel_target] [schedule_name]`",
        "- List: `node /skills/eventbridge-cron/list.js <user_id>`",
        "- Update: `node /skills/eventbridge-cron/update.js <user_id> <schedule_id> [--expression \"cron(...)\"] [--timezone \"TZ\"] [--message \"msg\"] [--enable] [--disable]`",
        "- Delete: `node /skills/eventbridge-cron/delete.js <user_id> <schedule_id>`",
        "",
        "Cron format: `cron(min hour day-of-month month day-of-week year)` — e.g., `cron(0 9 * * ? *)` for daily at 9 AM.",
        "Rate format: `rate(1 hour)`, `rate(5 minutes)`.",
        "",
        "## File Storage",
        "",
        "You have the **s3-user-files** skill for persistent file storage. Files survive across sessions.",
        "",
        "## Community Skills (ClawHub)",
        "",
        "The following community skills are pre-installed:",
        "- **jina-reader**: Extract web content as clean markdown (higher quality than built-in web_fetch)",
        "- **deep-research-pro**: In-depth multi-step research on complex topics (uses sub-agents)",
        "- **telegram-compose**: Rich HTML formatting for Telegram messages",
        "- **transcript**: YouTube video transcript extraction",
        "- **task-decomposer**: Break complex requests into manageable subtasks (uses sub-agents)",
        "",
        "### Installing More Skills",
        "",
        "You have the **clawhub-manage** skill to install/uninstall additional community skills from the ClawHub marketplace.",
        "When a user asks to install or add a skill, use this skill — do NOT say it's not possible or that exec is blocked.",
        "**Use Bash to run the skill scripts** (Bash is available, only exec is denied):",
        "- Install: `node /skills/clawhub-manage/install.js <skill-name>`",
        "- Uninstall: `node /skills/clawhub-manage/uninstall.js <skill-name>`",
        "- List: `node /skills/clawhub-manage/list.js`",
        "",
        "Installed skills are recorded per user and restored before requests are accepted in each new session.",
        "Installs never bypass ClawHub's security review: if install.js refuses a flagged skill, explain why to the user and do not retry.",
        "",
        "## API Key Storage",
        "",
        "You have the **api-keys** skill for secure API key storage.",
        "",
        "### Proactive Detection",
        "",
        "If a user message contains what looks like an API key or secret token — even without explicitly asking to save it — you MUST proactively offer to store it securely. Common patterns:",
        "- `sk-...`, `sk-proj-...` (OpenAI)",
        "- `key-...`, `pk-...` (generic)",
        "- `ghp_...`, `gho_...` (GitHub)",
        "- `xoxb-...`, `xoxp-...` (Slack)",
        "- `AKIA...` (AWS access key)",
        "- Any long alphanumeric string (20+ chars) that the user labels as a key, token, or secret",
        "",
        "When detected, say something like: *\"That looks like an API key. Let me store it securely so you don't lose it. I'll use Secrets Manager (recommended) — OK?\"* Then store it immediately using Secrets Manager unless the user prefers native storage. Infer the key name from context (e.g., `openai_api_key`, `github_token`).",
        "",
        "### Storage Options",
        "",
        "When a user explicitly asks to save an API key, present both options:",
        "",
        "**Option 1 — Native (file-based)**:",
        "- `node /skills/api-keys/native.js <user_id> set <key_name> <key_value>`",
        "- Stored in your workspace file, persists across sessions via S3 sync",
        "- KMS-encrypted at rest in S3, isolated to your user namespace",
        "",
        "**Option 2 — Secure (AWS Secrets Manager)** (recommended):",
        "- `node /skills/api-keys/secret.js <user_id> set <key_name> <key_value>`",
        "- Stored in AWS Secrets Manager, KMS-encrypted, auditable via CloudTrail",
        "- NOT stored in workspace files — stronger isolation",
        "",
        "**Unified retrieval** (checks SM first, falls back to native):",
        "- `node /skills/api-keys/retrieve.js <user_id> <key_name>`",
        "",
        "**Migration** between backends:",
        "- `node /skills/api-keys/migrate.js <user_id> <key_name> native-to-secure`",
        "- `node /skills/api-keys/migrate.js <user_id> <key_name> secure-to-native`",
        "",
        "Actions for both native.js and secret.js: `set`, `get`, `list`, `delete`",
        "",
        "**Important**: The `<user_id>` is your namespace (e.g. `telegram_12345`). Never write API keys to regular user files (s3-user-files). Always use the api-keys skill.",
        "",
        ...(browserEnabled
          ? [
              "## Browser (AgentCore Browser)",
              "",
              "You have the **agentcore-browser** skill for headless Chromium browsing. Use it when users ask to:",
              "- Visit or navigate to a web page",
              "- Take a screenshot of a website",
              "- Interact with page elements (click buttons, fill forms, scroll)",
              "",
              "**Commands** (run via Bash):",
              '- Navigate: `node /skills/agentcore-browser/navigate.js \'{"url": "https://example.com"}\'`',
              '- Screenshot: `node /skills/agentcore-browser/screenshot.js \'{"description": "Page screenshot"}\'`',
              '- Click: `node /skills/agentcore-browser/interact.js \'{"action": "click", "selector": "#btn"}\'`',
              '- Type: `node /skills/agentcore-browser/interact.js \'{"action": "type", "selector": "#input", "text": "hello"}\'`',
              '- Scroll: `node /skills/agentcore-browser/interact.js \'{"action": "scroll"}\'`',
              '- Wait: `node /skills/agentcore-browser/interact.js \'{"action": "wait", "selector": ".results"}\'`',
              "",
              "Screenshots are uploaded to S3 and delivered as images to the user's chat.",
              "The browser session is pre-created at startup — no setup needed.",
              "",
            ]
          : []),
        "## Sub-agents",
        "",
        "Skills like deep-research-pro and task-decomposer can spawn sub-agents for parallel work.",
        "Sub-agents share the same model and capabilities. Sandbox is disabled (the container is already isolated).",
        "",
      ].join("\n");
}

module.exports = { renderConfig, renderAgents, SUBAGENT_MODEL_NAME };
