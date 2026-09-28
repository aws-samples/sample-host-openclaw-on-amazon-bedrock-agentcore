# Project Structure

Full file tree of the repository. The [README](../README.md#project-structure) lists only the top-level directories.

```
openclaw-on-agentcore/
  app.py                          # CDK app entry point (8 stacks + opt-in OpenClawGateway)
  cdk.json                        # Configuration (model, budgets, sessions, cron, guardrails)
  requirements.txt                # Python deps (aws-cdk-lib, cdk-nag)
  stacks/
    __init__.py                   # Shared helper (RetentionDays converter)
    vpc_stack.py                  # VPC, subnets, NAT, 7 interface + 1 S3 gateway VPC endpoints, flow logs
    security_stack.py             # KMS CMK, Secrets Manager, Cognito, optional CloudTrail
    agentcore_stack.py            # Execution role, SG, S3 user-files bucket, optional AgentCore Browser (Runtime/ECR are toolkit-managed)
    router_stack.py               # Router Lambda + API Gateway HTTP API (telegram/slack/feishu routes) + DynamoDB identity
    observability_stack.py        # Operations dashboard, alarms, SNS topic, Bedrock invocation logging
    token_monitoring_stack.py     # Lambda processor, DynamoDB (3 GSIs), token analytics dashboard
    guardrails_stack.py           # Bedrock Guardrails (content filters, PII, topic denial)
    cron_stack.py                 # EventBridge Scheduler, Cron executor Lambda, IAM
    gateway_stack.py              # Prototype: AgentCore Gateway (MCP) + interceptor + tool Lambdas; only when enable_gateway=true
  bridge/
    Dockerfile                    # Container image (node:24-slim, ARM64, pinned openclaw@2026.9.5 + clawhub@0.23.3, 5 owner-pinned ClawHub skills)
    entrypoint.sh                 # Startup: configure IPv4, start contract server
    agentcore-contract.js         # AgentCore HTTP contract with hybrid routing (shim + OpenClaw)
    openclaw-tool-deny.test.js    # openclaw.json tools.deny covers the channel-delivery tools (node:test, 7 tests)
    read-body.js                  # Reads a request body as Buffers and decodes UTF-8 once (contract server keeps its 1 MB / 413 cap)
    read-body.test.js             # Request body decoding + size cap tests (node:test, 8 tests)
    legacy-session-import.js      # Pre-2.0 sessions.json import receipts (import runs once)
    legacy-session-import.test.js # Import receipt tests (node:test, 23 tests)
    gateway-mcp.js                # mcp.servers.agentcore config + bearer refresh (only when AGENTCORE_GATEWAY_URL is set)
    gateway-mcp.test.js           # Gateway MCP config/refresh + Cognito token provider tests (node:test, 12 tests)
    cognito-token.js              # Per-user Cognito ID/access token provider shared by proxy and contract server
    lightweight-agent.js          # Warm-up agent shim (17 tools: web, s3-user-files, eventbridge-cron, clawhub-manage, api-keys)
    lightweight-agent.test.js     # Lightweight agent unit tests (node:test, 125 tests)
    agentcore-proxy.js            # OpenAI -> Bedrock ConverseStream adapter + Identity + multimodal images
    image-support.test.js         # Image support unit tests (node:test)
    proxy-identity.test.js        # Proxy identity resolution tests (node:test)
    agentcore-browser.test.js     # Browser skill unit tests (node:test)
    browser-lifecycle.test.js     # Browser session lifecycle tests (node:test)
    content-extraction.test.js    # Content block extraction tests (node:test)
    subagent-routing.test.js      # Subagent model routing + detection tests (node:test)
    workspace-sync.js             # ~/.openclaw/ S3 sync (restore, change-driven + periodic saves, SQLite snapshots, gzip multipart for large ones)
    workspace-sync.test.js        # Workspace sync tests (node:test, 106 tests)
    state-storage.js              # Local state dir + session-storage mirror/restore (SQLite + workspace)
    state-storage.test.js         # State storage layout tests (node:test, 34 tests)
    scoped-credentials.js         # Per-user STS session-scoped S3 credentials
    scoped-credentials.test.js    # Scoped credentials unit tests (node:test, 43 tests)
    force-ipv4.js                 # DNS patch for Node.js Happy Eyeballs IPv6 issue
    cloudwatch-logger.js          # Ships container stdout/stderr to CloudWatch Logs
    CLAUDE.md                     # Project instructions (for Claude Code IDE)
    skills/
      s3-user-files/              # Custom per-user file storage skill (S3-backed)
      eventbridge-cron/           # Cron scheduling skill (EventBridge Scheduler)
      clawhub-manage/             # ClawHub skill installer (install/uninstall/list)
      api-keys/                   # Dual-mode API key management (native file + Secrets Manager)
        migrate.test.js           # migrate.js/native.js key-loss tests with a stubbed Secrets Manager (node:test, 9 tests)
      agentcore-browser/          # Optional headless browser skill (navigate/screenshot/interact)
  lambda/
    token_metrics/index.py        # Bedrock log -> DynamoDB + CloudWatch metrics
    router/index.py                    # Webhook router (Telegram + Slack + Feishu, image uploads)
    router/test_image_upload.py        # Image upload unit tests (pytest)
    router/test_content_extraction.py  # Content block extraction tests (pytest)
    router/test_telegram_chunking.py   # Telegram UTF-16 chunking + retry tests (pytest)
    router/test_markdown_html.py       # Markdown-to-HTML conversion tests (pytest)
    router/test_slack.py               # Slack handler tests (pytest)
    router/test_feishu.py              # Feishu handler tests (pytest)
    router/test_screenshot_handling.py # Screenshot marker delivery tests (pytest)
    router/test_formatting_integration.py # Formatting integration tests (pytest)
    cron/index.py                      # Cron executor (warmup, invoke, deliver to Telegram/Slack/Feishu)
    cron/test_telegram_chunking.py     # Telegram UTF-16 chunking + retry tests (pytest)
    cron/test_feishu_delivery.py       # Feishu delivery routing + tenant token tests (pytest)
    gateway_tools/                     # Prototype Gateway MCP targets (Node 22 Lambdas)
      tool-schemas.json                # MCP tool schemas consumed by CDK and the tests
      interceptor/index.js             # REQUEST interceptor: copies the bearer JWT into __caller_token
      s3_user_files/index.js           # user-files target: list/read/write/delete in the caller's S3 prefix
      eventbridge_cron/index.js        # schedules target: create/list/update/delete the caller's schedules
      lib/identity.js                  # JWT verification against the pool JWKS; namespace derivation
      lib/mcp.js                       # Lambda-target event/context helpers
      identity.test.js, tools.test.js  # Unit tests (node:test, 31 tests)
  scripts/
    setup-telegram.sh             # Telegram webhook + admin allowlist (one-step)
    setup-slack.sh                # Slack Event Subscriptions + admin allowlist
    setup-feishu.sh               # Feishu app credentials + event subscription + admin allowlist
    deploy.sh                     # Three-phase deploy (CDK -> Starter Toolkit -> CDK)
    e2e-deploy-and-test.sh        # Deploy then run the E2E suite
    manage-allowlist.sh           # Add/remove/list users in the allowlist
    agentcore-exec.py             # Operator CLI: run a shell command in a live session (InvokeAgentRuntimeCommand)
    guardrail-eval.py             # Operator CLI: run fixture prompts through ApplyGuardrail, exit 1 on unexpected verdicts
  tests/
    test_agentcore_exec.py        # Unit tests for scripts/agentcore-exec.py (mocked boto3, no AWS)
    test_guardrail_eval.py        # Unit tests for scripts/guardrail-eval.py (stubbed ApplyGuardrail, no AWS)
    test_state_bucket_lifecycle_synth.py # User-files bucket lifecycle rules synth test (no AWS)
    test_gateway_stack_synth.py   # OpenClawGateway synth tests: flag off = unchanged templates, flag on = stack + IAM + cdk-nag (10 tests, no AWS)
    e2e/                          # E2E tests (simulated Telegram webhooks + CloudWatch logs)
      config.py                   # AWS config auto-discovery (CF outputs, Secrets Manager)
      webhook.py                  # Build + POST Telegram webhook payloads
      session.py                  # DynamoDB session/user reset + AgentCore session stop
      log_tailer.py               # CloudWatch log tailing with pattern matching
      bot_test.py                 # CLI entrypoint + pytest test classes (50 tests, 15 classes)
      conftest.py                 # pytest fixtures, conversation scenarios
      container_logs.py           # Container/Lambda log helpers for the Gateway E2E tests
      test_gateway_tools.py       # Gateway MCP tools E2E (4 tests, `-m gateway`; skipped when the stack is not deployed)
  redteam/                        # LLM red team testing (promptfoo, 62 test cases)
  docs/
    images/architecture.svg       # README architecture diagram (AWS icons)
    architecture.md               # Solution architecture (ASCII diagrams)
    architecture-detailed.md      # Sequence diagrams, container internals, data flows
    openclaw-2.0-upgrade.md       # 2026.3.8 -> 2026.9.5 upgrade notes, risks, validation
    design-feishu-channel.md      # Feishu channel design
    security.md                   # Complete security architecture
    guardrails.md                 # Bedrock Guardrails operational runbook
    session-storage.md            # Persistent /mnt/workspace (Managed Session Storage)
    execute-command.md            # Operator CLI for InvokeAgentRuntimeCommand + security boundary
    gateway-mcp-tools.md          # Prototype: AgentCore Gateway MCP tools (design, identity flow, IAM, tests, live findings)
    registry-pilot.md             # Plan: AWS Agent Registry as approval gate + shared catalogue for Gateway MCP tools (no code)
```
