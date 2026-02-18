Implementation Plan — OpenClaw on AWS Bedrock AgentCore

Problem Statement:
The existing OpenClaw-on-AWS project deploys on a single EC2 instance managed via CloudFormation. We want to re-
architect it to run on Bedrock AgentCore's managed serverless runtime, gaining session isolation, built-in
identity/memory/observability, and eliminating EC2 patching/management — with production-secure posture and
granular token usage monitoring throughout.

Requirements:
- Strands Agents (Python) as the agent framework on AgentCore Runtime
- ECS Fargate sidecar for OpenClaw's messaging bridge (WhatsApp, Telegram, Discord, Slack)
- CloudFront + WAF + token auth for Web UI access
- CDKv2 (Python) for all infrastructure
- Production-secure: VPC endpoints, IAM least-privilege, encrypted storage, CloudTrail audit, no public agent
endpoints
- Granular token usage observability: per-user, per-channel, per-conversation, per-model token tracking with cost
estimation and budget alerts
- Start single-user, design for multi-user extensibility

Background:
- AgentCore Runtime provides serverless, session-isolated agent execution with pay-per-active-consumption pricing
(~$0.09/vCPU-hr, $0.0095/GB-hr, only during active processing — I/O wait is free)
- AgentCore Gateway transforms APIs/Lambda into MCP-compatible agent tools
- AgentCore Memory provides short-term (session) and long-term (cross-session) memory with built-in extraction
strategies
- AgentCore Identity handles OAuth/API key management for third-party tool access
- AgentCore Observability sends traces/metrics to CloudWatch via OpenTelemetry; CloudWatch GenAI Observability
provides pre-built dashboards for model invocations with token counts by model, daily token counts, input/output
breakdowns, and cost attribution
- Bedrock Model Invocation Logging provides per-request input/output token counts, model IDs, and latency — this
is the foundation for granular token tracking
- OpenClaw is a heavy token consumer due to multi-turn conversations, tool calls, long system prompts, and browser
/code execution — granular monitoring is essential for cost control

Proposed Solution:

mermaid
graph TB
    subgraph "Users"
        WA[WhatsApp]
        TG[Telegram]
        DC[Discord]
        SL[Slack]
        BR[Browser]
    end

    subgraph "Edge Layer"
        CF[CloudFront]
        WAF[AWS WAF]
    end

    subgraph "AWS VPC - Private"
        subgraph "Fargate Service"
            OC[OpenClaw Messaging Bridge<br/>ECS Fargate]
        end

        subgraph "AgentCore"
            RT[AgentCore Runtime<br/>Strands Agent]
            GW[AgentCore Gateway<br/>MCP Tools]
            MEM[AgentCore Memory<br/>Short+Long Term]
            ID[AgentCore Identity]
            OBS[AgentCore Observability]
        end

        subgraph "Token Monitoring"
            TM_FN[Token Metrics Lambda<br/>Process Invocation Logs]
            TM_DB[DynamoDB<br/>Token Usage Records]
            TM_DASH[CloudWatch Dashboard<br/>Token Analytics]
            TM_ALARM[Budget Alarms<br/>SNS Notifications]
        end

        subgraph "Data & Security"
            SM[Secrets Manager<br/>Tokens/Keys]
            CW[CloudWatch<br/>Logs/Traces/Metrics]
            CT[CloudTrail<br/>Audit]
        end

        subgraph "AI"
            BR_API[Bedrock Runtime<br/>Nova/Claude]
        end
    end

    WA & TG & DC & SL --> OC
    BR --> CF --> WAF --> OC
    OC -->|invoke agent| RT
    RT --> GW
    RT --> MEM
    RT --> ID
    RT --> OBS --> CW
    RT --> BR_API
    GW --> SM
    CT -.->|audit all calls| BR_API
    CW -->|invocation logs| TM_FN
    TM_FN --> TM_DB
    TM_DB --> TM_DASH
    TM_DASH --> TM_ALARM


Key architectural decisions:
1. Fargate runs the OpenClaw Node.js messaging bridge (containerized) — handles WebSocket connections, Web UI,
channel management
2. When a user message arrives, Fargate invokes the AgentCore Runtime endpoint for AI reasoning
3. AgentCore Runtime runs a Strands Agent that uses Bedrock models, with Memory for context and Gateway for tool
access
4. CloudFront + WAF protects the Web UI; no direct public access to Fargate or AgentCore
5. All secrets (gateway tokens, channel bot tokens) stored in Secrets Manager, never in code or env vars
6. User identity is modeled with actor_id in AgentCore Memory, enabling future multi-user isolation
7. Token monitoring pipeline: Bedrock invocation logs → CloudWatch Logs → Lambda (subscription filter) → DynamoDB
(per-user/channel/conversation aggregation) → CloudWatch custom metrics → dashboards + budget alarms

Task Breakdown:

Task 1: CDK Project Scaffolding & VPC Foundation
- Objective: Set up the CDKv2 Python project structure and deploy the foundational VPC with security controls
- Implementation:
  - Initialize CDK app with cdk init app --language python
  - Create a VPC stack with public/private subnets, NAT gateway, VPC flow logs enabled
  - Add VPC endpoints for Bedrock Runtime, SSM, ECR, Secrets Manager, CloudWatch Logs (Interface endpoints in
private subnets)
  - Configure security groups: Fargate SG (ingress from CloudFront only), VPC endpoint SG (ingress from Fargate SG
only)
  - Enable CloudTrail for the account with S3 log bucket (SSE-S3 encrypted, access logging enabled)
- Test: cdk synth produces valid template; cdk deploy creates VPC with all endpoints and flow logs visible in
CloudWatch
- Demo: VPC deployed with private subnets, VPC endpoints confirmed reachable, CloudTrail logging active

Task 2: AgentCore Strands Agent — Local Development & Deployment
- Objective: Build the core AI agent using Strands Agents and deploy it to AgentCore Runtime
- Implementation:
  - Create agent/ directory with my_agent.py using BedrockAgentCoreApp entrypoint pattern
  - Define system prompt for OpenClaw-style personal assistant behavior
  - Configure Bedrock model (default: global.amazon.nova-2-lite-v1:0) via environment variable
  - Add requirements.txt with strands-agents, strands-agents-tools, bedrock-agentcore,
bedrock-agentcore-starter-toolkit
  - Create IAM execution role in CDK with least-privilege: only bedrock:InvokeModel,
bedrock:InvokeModelWithResponseStream on specific model ARNs
  - Instrument the agent entrypoint with OpenTelemetry spans that tag each invocation with custom attributes:
openclaw.actor_id, openclaw.session_id, openclaw.channel (whatsapp/telegram/discord/slack/webui)
  - Use agentcore configure and agentcore deploy to push to AgentCore Runtime
  - Add CDK construct that creates the ECR repo and IAM role for the agent
- Test: agentcore dev locally, then agentcore invoke '{"prompt": "Hello"}' against deployed endpoint returns a
response; traces appear in CloudWatch with custom attributes
- Demo: Agent deployed on AgentCore Runtime, responds to prompts via CLI invocation, traces visible with channel/
user tags

Task 3: AgentCore Memory Integration
- Objective: Add short-term and long-term memory so the agent maintains conversation context and learns user
preferences
- Implementation:
  - Create AgentCore Memory store with semantic memory strategy (namespaced by actor_id)
  - In the agent entrypoint, accept actor_id and session_id in the payload
  - Before invoking the agent, load recent short-term events (list_events) and relevant long-term memories (
retrieve_memories) and inject into the prompt context
  - After agent response, store the interaction as a new event (create_event)
  - Add IAM permissions for AgentCore Memory API calls
- Test: Invoke agent twice in same session — second invocation references first conversation. New session
retrieves long-term facts from previous sessions
- Demo: Multi-turn conversation works across invocations; agent remembers user preferences across sessions

Task 4: Secrets Management & Security Hardening
- Objective: Set up Secrets Manager for all sensitive values and harden IAM policies
- Implementation:
  - Create Secrets Manager secrets via CDK for: gateway token, messaging channel bot tokens (WhatsApp, Telegram,
Discord, Slack placeholders)
  - Create a KMS CMK for encrypting secrets
  - Grant Fargate task role read-only access to specific secret ARNs (not *)
  - Grant AgentCore agent role zero access to secrets (it doesn't need them)
  - Add CDK Nag (cdk-nag) to the project for automated security checks on every synth
  - Ensure all S3 buckets have encryption, versioning, and public access blocked
- Test: cdk synth passes cdk-nag checks with no errors; Fargate task can read secrets; agent role cannot
- Demo: Secrets created in Secrets Manager, encrypted with CMK, accessible only by authorized roles. cdk-nag
report clean

Task 5: OpenClaw Messaging Bridge on ECS Fargate
- Objective: Containerize OpenClaw's messaging bridge and deploy on Fargate in the private VPC
- Implementation:
  - Create bridge/Dockerfile — Node.js base image, install openclaw@latest, configure for Bedrock mode
  - Create bridge/entrypoint.sh that reads secrets from Secrets Manager (via AWS SDK), writes config to
~/.openclaw/openclaw.json, starts the OpenClaw daemon
  - CDK: ECS Cluster, Fargate task definition (256 CPU / 512 MiB), Fargate service in private subnet
  - Fargate task role: read Secrets Manager, write CloudWatch Logs, invoke AgentCore Runtime endpoint
  - No public IP on Fargate tasks — all traffic via VPC endpoints or NAT gateway (for external messaging APIs)
  - Configure OpenClaw to use a custom "Bedrock AgentCore" provider that forwards AI requests to the AgentCore
Runtime endpoint instead of direct Bedrock calls
- Test: Fargate task starts, OpenClaw Web UI accessible via port 18789 internally, health check passes
- Demo: Fargate service running, OpenClaw logs visible in CloudWatch, container healthy

Task 6: CloudFront + WAF for Secure Web UI Access
- Objective: Expose the OpenClaw Web UI securely via CloudFront with WAF protection
- Implementation:
  - CDK: Application Load Balancer (internal) in private subnets, target group pointing to Fargate service port
18789
  - CDK: CloudFront distribution with ALB as origin, HTTPS-only, TLS 1.2 minimum
  - CDK: WAF WebACL attached to CloudFront with rules: rate limiting (100 req/5min per IP), AWS managed rule
groups (Common Rule Set, Known Bad Inputs, IP Reputation), geo-restriction (optional)
  - Token-based auth: CloudFront function that validates ?token= query parameter against a hashed value stored in
CloudFront Function KV or a Lambda@Edge that checks against Secrets Manager
  - Add custom response headers: X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Strict-Transport-Security
- Test: Access CloudFront URL with valid token — Web UI loads. Invalid token — 403. Rate limit triggers on rapid
requests
- Demo: Web UI accessible via HTTPS CloudFront URL, WAF blocking invalid requests, security headers present

Task 7: Fargate ↔ AgentCore Runtime Integration
- Objective: Wire the messaging bridge to invoke the AgentCore agent for AI reasoning
- Implementation:
  - Create a lightweight proxy/adapter (Python Lambda or inline in the OpenClaw config) that translates OpenClaw's
Bedrock API calls into AgentCore Runtime invocations
  - The adapter accepts the user message + actor_id (derived from messaging channel user ID) + session_id +
channel (whatsapp/telegram/discord/slack/webui) and calls the AgentCore endpoint
  - Configure OpenClaw's model provider to point to this adapter endpoint
  - Ensure the Fargate task role has permission to invoke the AgentCore Runtime endpoint
  - Add retry logic with exponential backoff for AgentCore invocations
- Test: Send a message via the Web UI chat → message flows through Fargate → AgentCore → Bedrock → response
appears in UI
- Demo: End-to-end message flow working: type in Web UI, get AI response powered by AgentCore

Task 8: AgentCore Observability & Bedrock Invocation Logging Foundation
- Objective: Enable full observability pipeline and Bedrock model invocation logging as the foundation for
granular token monitoring
- Implementation:
  - Enable observability in AgentCore agent configuration (default with agentcore create)
  - Set up X-Ray trace segment destination to CloudWatch Logs (
aws xray update-trace-segment-destination --destination CloudWatchLogs)
  - Enable Bedrock Model Invocation Logging via CDK custom resource:
    - Log destination: CloudWatch Logs log group /aws/bedrock/invocation-logs
    - Include input/output token counts, model ID, request metadata
    - Create IAM service role for Bedrock to write to CloudWatch Logs
  - Create CDK CloudWatch dashboard ("OpenClaw Operations") with:
    - AgentCore session count, latency (p50/p95/p99), error rate
    - Fargate CPU/memory utilization
    - Bedrock invocation count and throttle rate
  - Add CloudWatch alarms: error rate > 5%, p99 latency > 10s, Fargate unhealthy task count > 0, Bedrock throttle
rate > 1%
  - SNS topic for alarm notifications
  - Fargate container logs → CloudWatch Log Group with 30-day retention
  - Verify CloudWatch GenAI Observability pre-built dashboards populate (Model Invocations view with token counts
by model, daily token counts)
- Test: Invoke agent, verify traces appear in CloudWatch X-Ray, Bedrock invocation logs appear in log group with
token counts, pre-built GenAI dashboard shows data
- Demo: CloudWatch operational dashboard live, GenAI observability dashboard showing model invocation metrics,
traces visible in X-Ray, alarms configured

Task 9: Granular Token Usage Monitoring & Budget Alerts
- Objective: Build a custom token analytics pipeline that provides per-user, per-channel, per-conversation, and
per-model token tracking with cost estimation and budget alerts
- Implementation:
  - **Token Metrics Lambda**: Create a Lambda function triggered by CloudWatch Logs subscription filter on the
Bedrock invocation log group (/aws/bedrock/invocation-logs). For each invocation log entry, extract:
    - inputTokenCount, outputTokenCount, modelId (from Bedrock log)
    - openclaw.actor_id, openclaw.session_id, openclaw.channel (from the request metadata / trace correlation)
    - Compute estimated cost using a model pricing lookup table (e.g., Nova Lite: $0.30/$2.50 per 1M input/output
tokens)
  - **DynamoDB Token Usage Table**: Single table design with composite keys enabling multiple query patterns:
    - PK: USER#<actor_id>, SK: DATE#<yyyy-mm-dd>#CHANNEL#<channel>#SESSION#<session_id>
    - GSI1: PK: CHANNEL#<channel>, SK: DATE#<yyyy-mm-dd> (for per-channel aggregation)
    - GSI2: PK: MODEL#<model_id>, SK: DATE#<yyyy-mm-dd> (for per-model aggregation)
    - GSI3: PK: DATE#<yyyy-mm-dd>, SK: COST#<estimated_cost> (for daily cost ranking)
    - Attributes: inputTokens, outputTokens, totalTokens, estimatedCostUSD, invocationCount, channel, modelId
    - TTL attribute for automatic cleanup (default 90 days)
    - DynamoDB encryption at rest with AWS-managed key
  - **CloudWatch Custom Metrics**: Lambda publishes custom metrics to OpenClaw/TokenUsage namespace:
    - InputTokens (dimensions: ActorId, Channel, ModelId)
    - OutputTokens (dimensions: ActorId, Channel, ModelId)
    - EstimatedCostUSD (dimensions: ActorId, Channel, ModelId)
    - InvocationCount (dimensions: ActorId, Channel, ModelId)
  - **Token Analytics Dashboard** ("OpenClaw Token Analytics"): CDK CloudWatch dashboard with:
    - Total tokens consumed (input vs output) — line graph, 1-hour period
    - Token usage by channel (WhatsApp vs Telegram vs Discord vs Slack vs WebUI) — stacked area chart
    - Token usage by model (Nova Lite vs Claude Sonnet vs etc.) — pie chart / bar chart
    - Top 5 users by token consumption — bar chart (ready for multi-user)
    - Estimated daily/weekly/monthly cost — number widget + trend line
    - Average tokens per conversation — useful for identifying verbose conversations
    - Requests grouped by input token size (small <1K, medium 1K-10K, large 10K-50K, huge >50K) — histogram
  - **Budget Alerts**: CloudWatch alarms on custom metrics:
    - Daily token budget alarm: total tokens > configurable threshold (default 1M tokens/day) → SNS notification
    - Daily cost budget alarm: estimated cost > configurable threshold (default $5/day) → SNS notification
    - Anomaly detection alarm: token usage deviates > 2 standard deviations from 7-day baseline → SNS notification
(catches runaway conversations or prompt injection)
    - Per-user token spike alarm: single user consuming > 50% of daily budget → SNS notification (ready for multi-
user)
  - **Lambda IAM**: Least-privilege — read CloudWatch Logs, write DynamoDB, publish CloudWatch metrics. No access
to Secrets Manager or Bedrock
  - **CDK configuration**: All thresholds (daily token budget, daily cost budget, anomaly band width, TTL days)
exposed as CDK context variables so operators can tune without code changes
- Test: Invoke agent 10+ times with different prompts (short and long). Verify: DynamoDB records created with
correct token counts, CloudWatch custom metrics populated, dashboard graphs render, trigger budget alarm by
setting threshold low
- Demo: Token Analytics dashboard showing real-time token consumption broken down by channel/model/user. Budget
alarm fires when threshold exceeded. DynamoDB queryable for historical analysis (e.g., "show me all token usage
for WhatsApp last 7 days")

Task 10: End-to-End Integration Testing & Messaging Channel Wiring
- Objective: Connect messaging channels and validate the complete flow from phone to AI response with full
observability
- Implementation:
  - Document the channel setup process: how to scan WhatsApp QR code via CloudFront Web UI, how to add Telegram/
Discord/Slack bot tokens via the UI (tokens stored in Secrets Manager)
  - Create a scripts/deploy.sh helper that runs cdk deploy --all and outputs the CloudFront URL + initial gateway
token
  - Create README.md with architecture diagram, deployment instructions, security features, cost estimates, token
monitoring guide
  - Add a scripts/rotate-token.sh that generates a new gateway token, updates Secrets Manager, and restarts the
Fargate task
  - Validate full observability chain: send message → verify in CloudWatch GenAI dashboard (model invocations) →
verify in Token Analytics dashboard (per-user/channel breakdown) → verify in DynamoDB (raw records) → verify
budget alarm fires at threshold
  - Validate: WhatsApp message → Fargate → AgentCore → Bedrock → response back to WhatsApp
- Test: Full end-to-end test with at least one messaging channel (Web UI chat at minimum). Verify CloudTrail shows
Bedrock API calls, CloudWatch shows agent traces, WAF logs show request filtering, Token Analytics dashboard
shows the interaction with correct token counts and cost estimate
- Demo: Complete working system — send a message from a phone (or Web UI), receive AI-powered response. Two
dashboards live: Operations (health/latency/errors) and Token Analytics (usage/cost/budgets). Security controls
verified: WAF active, secrets encrypted, no public endpoints exposed, audit trail complete. Budget alerts
configured and tested
