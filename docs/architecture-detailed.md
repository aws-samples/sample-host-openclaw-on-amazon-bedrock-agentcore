# Detailed Technical Architecture

This document provides a detailed technical view of the OpenClaw on AgentCore architecture. For a high-level overview, see the [README](../README.md#architecture).

AgentCore Runtime V2 is the only platform. See the [AgentCore Runtime V2 runbook](runtime-v2.md) for snapshot eligibility, user readiness, deployment and migration limits.

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

        GUARDRAIL[Bedrock Guardrails]
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
    PROXY -->|"ConverseStream (+guardrailConfig)"| BEDROCK

    SCHED -->|trigger| CRONLAMBDA
    CRONLAMBDA -->|invoke| CONTRACT
    CRONLAMBDA -->|reply| TG & SL
```

## Component Details

| Component | Port | Purpose |
|---|---|---|
| **Contract Server** | 8080 | AgentCore HTTP contract (`/ping`, `/invocations`); V2 bootstrap/activation and full-gateway admission |
| **Bedrock Proxy** | 18790 | OpenAI-compatible API → Bedrock ConverseStream, Cognito identity, multimodal image handling |
| **OpenClaw Gateway** | 18789 | Headless OpenClaw `2026.9.5` with full tools and ClawHub skills; V2 verifies `/startupz`, protocol-4 token handshake and cached `last-heartbeat` RPC |
| **Shared routing layer** | — | Conditional Router/Cron admission on one AgentCore Runtime ARN, endpoint name, session ID and generation |

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

    RL->>DB: Resolve route and conditionally admit invocation
    RL->>AC: InvokeAgentRuntime (ARN, endpoint name, session, generation)
    Note over RL,C: Typing indicator every 4s (Telegram)<br/>Progress message after 30s (both channels)

    opt First message (cold start)
        AC->>AC: STS AssumeRole (scoped S3 creds)
        AC->>S3: Restore .openclaw/ after session-storage restore
        Note over AC,S3: V2 restore failure/timeout blocks activation
        AC->>AC: Start proxy and OpenClaw with scoped creds
    end

    Note over AC,B: V2 waits for USER_READY<br/>Full OpenClaw through the authenticated gateway
    AC->>B: ConverseStream (via OpenClaw → proxy)
    B-->>AC: Completed response

    AC-->>RL: Final response
    Note over RL: Unwrap nested content blocks<br/>Convert markdown → Telegram HTML
    RL->>C: Send message (HTML formatted)
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
    AC-->>CL: Ready (V2 requires USER_READY)
    CL->>AC: Send cron message
    AC->>B: ConverseStream
    B-->>AC: Response
    AC-->>CL: Final response
    CL->>C: Deliver to user
```

### Cross-Channel Account Linking

An unregistered channel can become an alias of the canonical V2 user. Its caller/delivery identity stays separate from SESSION.actorId. An actor already owned by another canonical user cannot transfer; this is not an account merge.

```mermaid
sequenceDiagram
    participant U as User
    participant TG as Telegram
    participant SL as Slack
    participant RL as Router Lambda
    participant DB as DynamoDB

    U->>TG: "link"
    TG->>RL: Webhook
    RL->>DB: Create BIND#A1B2C3D4 (10 min TTL)
    RL->>TG: "Code: A1B2C3D4"
    TG->>U: Display code

    U->>SL: "link A1B2C3D4"
    SL->>RL: Webhook
    RL->>DB: Lookup BIND#A1B2C3D4
    RL->>DB: Create CHANNEL#slack:U123 → same userId
    RL->>DB: Delete BIND#A1B2C3D4
    RL->>SL: "Accounts linked!"
    SL->>U: Confirmation
```

## Container Internals

V2 completes strict activation before full OpenClaw handles chat or cron.

```mermaid
flowchart TB
    subgraph MicroVM["AgentCore V2 microVM: canonical user"]
        CONTRACT["Contract :8080<br/>Full readiness + immutable binding"]
        CONTRACT -->|"USER_READY"| OPENCLAW["OpenClaw :18789<br/>Full tools, skills, subagents"]
        OPENCLAW -->|"/v1/chat/completions"| PROXY["Bedrock proxy :18790<br/>Credentials and Guardrails"]
    end
    PROXY --> BEDROCK["Amazon Bedrock"]
    CONTRACT <-->|"Strict restore / persistence"| S3[("S3 + session storage mirror")]
```

The OpenAI-compatible `/v1/chat/completions` protocol is independent of AgentCore Runtime platform V2. File, scheduling, ClawHub and API key skills remain in full OpenClaw; Browser and Gateway integrations retain their readiness checks.

### Two-Phase Startup

```mermaid
flowchart LR
    B["Common bootstrap<br/>User-neutral live gateway"] --> S["BOOTSTRAP_READY<br/>Snapshot boundary"]
    S --> R["First user request<br/>Immutable binding"]
    R --> A["Strict restore<br/>Scoped credentials"]
    A --> G["Bind eligible prepared gateway<br/>or start gateway for restored/custom state"]
    G --> P["Browser, skills<br/>Persistence"]
    P --> U["USER_READY<br/>Full chat / cron"]
```

See the [AgentCore Runtime V2 lifecycle contract](runtime-v2.md#lifecycle-and-compatibility) for snapshot eligibility, readiness checks and restore failure handling.

Deployment warmup is a billed real chat with an isolated synthetic actor/session after AgentCore Runtime and endpoint readback. It requires full OpenClaw completion, USER_READY and matching immutable binding; see the [warmup contract](runtime-v2.md#post-deployment-warmup). It neither warms real-user state nor establishes first-reply latency.

## S3 Bucket Structure

```
s3://openclaw-user-files-{account}-{region}/
├── telegram_123456789/           # User namespace (channel_id)
│   ├── .openclaw/                 # Workspace (synced on init/shutdown)
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
| `ACTOR#telegram:123` | `OWNER` | Durable userId, actorId, platformVersion and ownerGeneration | - |
| `USER#user_abc` | `PROFILE` | User profile | - |
| `USER#user_abc` | `CHANNEL#telegram:123` | User's bound channels | - |
| `USER#user_abc` | `SESSION` | Route tuple, platform, admission status/count/tokens; V2 actor binding | - |
| `USER#user_abc` | `CRON#reminder-1` | Cron schedule metadata | - |
| `BIND#ABC123` | `BIND` | Cross-channel bind code | 10 min |
| `ALLOW#telegram:123` | `ALLOW` | User allowlist entry | - |

Route fields are `runtimeArn`, `endpointName`, `sessionId`, `generation`, `platformVersion`, `routeStatus`, `inFlight` and `activeAdmissions`; V2 also pins `actorId`, AgentCore Runtime version, image digest and configuration hash. V2 first registrations transact five absent identity/session/OWNER records. V2 starts at generation 1. Missing OWNER requires a strongly consistent paginated legacy Scan; retained provenance or uncertainty blocks fresh enrollment. OWNER has no TTL. A missing V2-enrolled SESSION/profile fails closed. V1 and incomplete routes fail closed; there is no default AgentCore Runtime fallback.

Admission adds a unique token and increments the count; release removes only that token once. Unknown transport/deadline outcomes retain admission, and freeze requires zero count plus no tokens. Cron's bounded calls reserve 6 seconds for release, plus 15 seconds for delivery on business calls. Conditional admission does not fence S3 writes, stop processes or prove drain. Unregistered channel aliases may link to the canonical V2 route; transfer from another canonical owner and existing-user migration are not implemented. See the [runbook](runtime-v2.md) for reconciliation limits.

## Security Architecture

See [security.md](security.md) for the complete security architecture.

**Key controls:**
- VPC isolation with 7 VPC endpoints
- Webhook signature validation (Telegram + Slack)
- Per-user microVM isolation
- STS session-scoped credentials (per-user S3 namespace + DynamoDB record restriction)
- KMS encryption at rest
- Security group egress restricted to HTTPS (TCP 443) only
- OpenClaw `read` tool denied (prevents credential access); `exec` allowed for skill management (STS-scoped); proxy bound to loopback
- Least-privilege IAM with cdk-nag enforcement
