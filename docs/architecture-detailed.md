# Detailed Technical Architecture

This document provides a detailed technical view of the OpenClaw on AgentCore architecture. For a high-level overview, see the [README](../README.md#architecture).

## Component Diagram

```mermaid
flowchart TB
    subgraph Channels
        TG[Telegram]
        SL[Slack]
    end

    subgraph AWS
        APIGW[API Gateway]

        subgraph Router[Router Lambda]
            WH[Webhook Handler]
            UR[User Resolution]
        end

        DDB[(DynamoDB)]
        S3[(S3)]

        subgraph AgentCore[AgentCore Runtime]
            CONTRACT[Contract :8080]
            PROXY[Proxy :18790]
            OPENCLAW[OpenClaw :18789]
        end

        BEDROCK[Bedrock Claude]

        subgraph Cron[EventBridge Scheduler]
            SCHED[Schedules]
            CRONLAMBDA[Cron Executor Lambda]
        end
    end

    TG & SL -->|webhook| APIGW
    APIGW --> WH
    WH --> UR
    UR <-->|identity| DDB
    UR -->|images| S3
    UR -->|invoke| CONTRACT
    CONTRACT <-->|workspace| S3
    CONTRACT --> PROXY
    CONTRACT <-->|WebSocket| OPENCLAW
    PROXY -->|ConverseStream| BEDROCK

    SCHED -->|trigger| CRONLAMBDA
    CRONLAMBDA -->|invoke| CONTRACT
    CRONLAMBDA -->|reply| TG & SL
```

## Component Details

| Component | Port | Purpose |
|---|---|---|
| **Contract Server** | 8080 | AgentCore HTTP contract (`/ping`, `/invocations`), lazy initialization, routing (shim vs WebSocket bridge) |
| **Lightweight Agent** | — | Warm-up shim during cold start; agentic loop with 10 tools via proxy → Bedrock (see below) |
| **Bedrock Proxy** | 18790 | OpenAI-compatible API → Bedrock ConverseStream, Cognito identity, multimodal image handling |
| **OpenClaw Gateway** | 18789 | Headless AI agent with full tools and ClawHub skills (available after ~2-4 min startup) |

## Data Flow

### Message Flow (User → Agent → Response)

```mermaid
sequenceDiagram
    participant U as User
    participant C as Channel
    participant AG as API Gateway
    participant RL as Router Lambda
    participant DB as DynamoDB
    participant S3 as S3
    participant AC as AgentCore
    participant B as Bedrock

    U->>C: Send message
    C->>AG: Webhook POST
    AG->>RL: Invoke
    RL-->>C: 200 OK (immediate)
    RL->>RL: Self-invoke async
    RL->>DB: Resolve user identity

    opt Has image
        RL->>C: Download image
        RL->>S3: Upload to _uploads/
    end

    RL->>AC: InvokeAgentRuntime

    opt First message (cold start)
        AC->>AC: Start proxy (~5s)
        AC->>S3: Restore .openclaw/ (background)
        AC->>AC: Start OpenClaw (background, ~2-4 min)
    end

    alt Warm-up phase (OpenClaw not ready)
        Note over AC,B: Lightweight agent shim handles message
        AC->>B: ConverseStream (via proxy)
        B-->>AC: Response + warm-up footer
    else Full mode (OpenClaw ready)
        Note over AC,B: WebSocket bridge to OpenClaw
        AC->>B: ConverseStream (via OpenClaw → proxy)
        B-->>AC: Response (no footer)
    end

    AC-->>RL: Final response
    RL->>C: Send message
    C->>U: Display response
```

### Cron Job Flow (Scheduled Task)

```mermaid
sequenceDiagram
    participant EB as EventBridge
    participant CL as Cron Lambda
    participant AC as AgentCore
    participant B as Bedrock
    participant C as Channel

    EB->>CL: Scheduled trigger
    CL->>AC: Warmup request
    AC-->>CL: Ready
    CL->>AC: Send cron message
    AC->>B: ConverseStream
    B-->>AC: Response
    AC-->>CL: Final response
    CL->>C: Deliver to user
```

### Cross-Channel Account Linking

```mermaid
sequenceDiagram
    participant U as User
    participant TG as Telegram
    participant SL as Slack
    participant RL as Router Lambda
    participant DB as DynamoDB

    U->>TG: "link"
    TG->>RL: Webhook
    RL->>DB: Create BIND#ABC123 (10 min TTL)
    RL->>TG: "Code: ABC123"
    TG->>U: Display code

    U->>SL: "link ABC123"
    SL->>RL: Webhook
    RL->>DB: Lookup BIND#ABC123
    RL->>DB: Create CHANNEL#slack:U123 → same userId
    RL->>DB: Delete BIND#ABC123
    RL->>SL: "Accounts linked!"
    SL->>U: Confirmation
```

## Container Internals

```mermaid
flowchart TB
    subgraph MicroVM["AgentCore MicroVM (ARM64, per-user)"]
        CONTRACT["<b>Contract Server :8080</b><br/>GET /ping → Healthy<br/>POST /invocations<br/>Lazy init · SIGTERM save"]

        CONTRACT -->|"warm-up<br/>(OpenClaw not ready)"| SHIM
        CONTRACT -->|"full mode<br/>(OpenClaw ready)"| OPENCLAW

        subgraph ShimBox["Warm-up Phase (~5s – ~2-4min)"]
            SHIM["<b>Lightweight Agent</b><br/>Agentic loop (20 iters)<br/>10 tools · SSRF protection<br/>Appends warm-up footer"]
        end

        subgraph FullBox["Full Mode (~2-4min onward)"]
            OPENCLAW["<b>OpenClaw Gateway :18789</b><br/>Headless mode · Full tool profile<br/>5 ClawHub skills · Sub-agents"]
        end

        PROXY["<b>Bedrock Proxy :18790</b><br/>OpenAI compat → ConverseStream<br/>Cognito identity · Multimodal images"]

        SHIM -->|"POST /v1/chat/completions<br/>(non-streaming)"| PROXY
        OPENCLAW <-->|WebSocket| PROXY
    end

    PROXY -->|ConverseStream| BEDROCK["Amazon Bedrock<br/>Claude"]

    S3[("S3<br/>workspace · files · images")]
    CONTRACT <-->|"restore / save<br/>.openclaw/"| S3
    SHIM -.->|"execFile<br/>skill scripts"| S3
```

### Lightweight Agent Tools

```mermaid
flowchart LR
    subgraph WebTools["In-Process (HTTP)"]
        WF["web_fetch<br/><i>Read web pages</i>"]
        WS["web_search<br/><i>DuckDuckGo HTML</i>"]
    end

    subgraph FileTools["Child Process (execFile)"]
        RF["read_user_file"]
        WUF["write_user_file"]
        LF["list_user_files"]
        DF["delete_user_file"]
    end

    subgraph CronTools["Child Process (execFile)"]
        CS["create_schedule"]
        LS["list_schedules"]
        US["update_schedule"]
        DS["delete_schedule"]
    end

    subgraph SSRF["SSRF Prevention"]
        BL["Hostname blocklist<br/><i>localhost, metadata, IMDS</i>"]
        DNS["Post-DNS IP check<br/><i>loopback, RFC-1918,<br/>RFC-6598, link-local,<br/>IPv6 ULA, IPv4-mapped</i>"]
        LIM["Limits: 512KB raw,<br/>50KB text, 15s timeout,<br/>3 redirects, 8 results"]
    end

    WF & WS --> BL --> DNS --> LIM
    FileTools -->|"/skills/s3-user-files/*.js"| S3[("S3")]
    CronTools -->|"/skills/eventbridge-cron/*.js"| EB["EventBridge<br/>Scheduler"]
```

### Two-Phase Startup

```mermaid
gantt
    title Cold Start Timeline
    dateFormat s
    axisFormat %S s

    section Container
    MicroVM created                     :done, t0, 0, 1s

    section Warm-up Phase
    Proxy starts (~5s)                  :active, t1, 1s, 5s
    Lightweight agent handles messages  :active, t2, 5s, 150s

    section Background
    OpenClaw starting (~2-4 min)        :crit, t3, 5s, 150s
    Workspace restore from S3           :done, t4, 1s, 10s

    section Full Mode
    OpenClaw ready — handoff            :milestone, m1, 150s, 0
    Full runtime handles messages       :t5, 150s, 200s
```

**Warm-up phase** (t=~5s to ~2-4min): Lightweight agent responds with 10 tools (web_fetch, web_search, 4 file, 4 cron). All responses include `"_Warm-up mode — after full startup..._"` footer.

**Full mode** (t=~2-4min onward): OpenClaw gateway handles messages via WebSocket bridge. No warm-up footer. ClawHub skills available (transcript, deep-research-pro, jina-reader, telegram-compose, task-decomposer).

### Lightweight Agent Architecture

The lightweight agent (`bridge/lightweight-agent.js`) provides immediate responsiveness during the ~2-4 minute OpenClaw cold start. It is NOT a replacement for OpenClaw — it's a shim that handles messages until the full runtime is ready.

| Property | Detail |
|---|---|
| **Routing** | Calls proxy at `127.0.0.1:18790/v1/chat/completions` (OpenAI format, non-streaming) |
| **Agentic loop** | Up to 20 iterations of tool-call → tool-result → assistant-response |
| **Tools (10)** | `read_user_file`, `write_user_file`, `list_user_files`, `delete_user_file`, `create_schedule`, `list_schedules`, `update_schedule`, `delete_schedule`, `web_fetch`, `web_search` |
| **File/cron tools** | Execute skill scripts via `execFile` with isolated env vars |
| **Web tools** | In-process HTTP(S) with SSRF prevention (blocked IPs, DNS rebinding mitigation, redirect validation) |
| **SSRF protection** | Pre-connection hostname blocklist + post-DNS-resolution IP validation covering loopback, RFC-1918, RFC-6598, link-local (AWS IMDS), IPv6 ULA, IPv4-mapped IPv6 |
| **Web limits** | 512KB raw HTML, 50KB text output, 15s timeout, 3 redirect max, 8 search results |
| **Detection** | Appends deterministic `"_Warm-up mode — ..."` footer to every response; absence of footer = OpenClaw is handling messages |
| **Handoff** | Contract server checks `openclawReady` flag; once true, all messages route via WebSocket bridge to OpenClaw |

## S3 Bucket Structure

```
s3://openclaw-user-files-{account}-{region}/
├── telegram_123456789/           # User namespace (channel_id)
│   ├── .openclaw/                 # Workspace (synced on init/shutdown)
│   │   ├── openclaw.json
│   │   ├── MEMORY.md
│   │   ├── USER.md
│   │   └── ...
│   ├── _uploads/                  # Image uploads (from Router Lambda)
│   │   ├── img_1709012345_a1b2.jpeg
│   │   └── ...
│   └── documents/                 # User files (via s3-user-files skill)
│       └── notes.md
├── slack_U12345678/
│   └── ...
└── ...
```

## DynamoDB Schema

**Table: `openclaw-identity`**

| PK | SK | Purpose | TTL |
|---|---|---|---|
| `CHANNEL#telegram:123` | `PROFILE` | Channel → userId mapping | - |
| `USER#user_abc` | `PROFILE` | User profile | - |
| `USER#user_abc` | `CHANNEL#telegram:123` | User's bound channels | - |
| `USER#user_abc` | `SESSION` | Current AgentCore session ID | - |
| `USER#user_abc` | `CRON#reminder-1` | Cron schedule metadata | - |
| `BIND#ABC123` | `BIND` | Cross-channel bind code | 10 min |
| `ALLOW#telegram:123` | `ALLOW` | User allowlist entry | - |

## Security Architecture

See [SECURITY.md](../SECURITY.md) for comprehensive security documentation.

**Key controls:**
- VPC isolation with 7 VPC endpoints
- Webhook signature validation (Telegram + Slack)
- Per-user microVM isolation
- KMS encryption at rest
- Least-privilege IAM with cdk-nag enforcement
