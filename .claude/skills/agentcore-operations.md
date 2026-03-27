---
name: agentcore-operations
description: "OpenClaw on AgentCore Runtime operations — deployment (CDK + Starter Toolkit hybrid), Docker build/push, runtime management, session lifecycle, debugging, adding new channels, and common gotchas. Use this skill for deployment, troubleshooting, operations questions, adding new messaging channels, or when something isn't working."
user-invocable: true
---

# OpenClaw on AgentCore — Operations Guide

## Hybrid Deployment Architecture

CDK manages infrastructure. AgentCore Starter Toolkit manages the container runtime.

| Component | Managed By |
|-----------|-----------|
| VPC, NAT, VPC Endpoints | CDK (`OpenClawVpc`) |
| KMS, Secrets, Cognito | CDK (`OpenClawSecurity`) |
| ECR repo, S3, SG, IAM Role | CDK (`OpenClawAgentCore`) |
| Router Lambda, API Gateway, DynamoDB | CDK (`OpenClawRouter`) |
| Cron Lambda, EventBridge Scheduler | CDK (`OpenClawCron`) |
| Dashboards, Alarms | CDK (`OpenClawObservability`, `OpenClawTokenMonitoring`) |
| **Runtime, Endpoint, Docker image** | **Starter Toolkit / direct API** |

### Why hybrid?
- AgentCore Runtime is ARM64 — local x86 machines can't easily cross-compile
- Starter Toolkit supports CodeBuild (cloud ARM64 build) and local-build modes
- CDK's CfnRuntime has L1 property gaps; direct API gives full control
- AgentCore console only shows metrics for Starter Toolkit-created runtimes

---

## Deployment Playbook

### 1. CDK Infrastructure Deploy

```bash
source .venv/bin/activate
export CDK_DEFAULT_ACCOUNT=<ACCOUNT_ID> CDK_DEFAULT_REGION=us-west-2
cdk deploy --all --require-approval never
```

### 2. Build & Push Docker Image

```bash
# Build locally (ARM64)
cd bridge && sudo docker build --platform linux/arm64 -t openclaw-bridge:v${TAG} .

# Login to ECR
aws ecr get-login-password --region us-west-2 | \
  sudo docker login --username AWS --password-stdin \
  <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com

# Tag & push
ECR=<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent
sudo docker tag openclaw-bridge:v${TAG} ${ECR}:v${TAG}
sudo docker push ${ECR}:v${TAG}
```

### 3. Update Runtime

**CRITICAL: `update-agent-runtime` is a FULL REPLACE. Omitting `--environment-variables` wipes ALL env vars.**

```bash
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> \
  --role-arn "arn:aws:iam::<ACCOUNT_ID>:role/openclaw-agentcore-execution-role-us-west-2" \
  --agent-runtime-artifact "{\"containerConfiguration\":{\"containerUri\":\"${ECR}:v${TAG}\"}}" \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"securityGroups":["<SECURITY_GROUP_ID>"],"subnets":["<PRIVATE_SUBNET_1>","<PRIVATE_SUBNET_2>"]}}' \
  --environment-variables '{
    "AWS_REGION":"us-west-2",
    "BEDROCK_AGENTCORE_MEMORY_ID":"<MEMORY_ID>",
    "BEDROCK_AGENTCORE_MEMORY_NAME":"openclaw_agent_mem",
    "BEDROCK_MODEL_ID":"global.anthropic.claude-opus-4-6-v1",
    "COGNITO_CLIENT_ID":"<COGNITO_CLIENT_ID>",
    "COGNITO_PASSWORD_SECRET_ID":"openclaw/cognito-password-secret",
    "COGNITO_USER_POOL_ID":"<COGNITO_USER_POOL_ID>",
    "CRON_LAMBDA_ARN":"arn:aws:lambda:us-west-2:<ACCOUNT_ID>:function:openclaw-cron-executor",
    "CRON_LEAD_TIME_MINUTES":"5",
    "EVENTBRIDGE_ROLE_ARN":"arn:aws:iam::<ACCOUNT_ID>:role/openclaw-cron-scheduler-role-us-west-2",
    "EVENTBRIDGE_SCHEDULE_GROUP":"openclaw-cron",
    "EXECUTION_ROLE_ARN":"arn:aws:iam::<ACCOUNT_ID>:role/openclaw-agentcore-execution-role-us-west-2",
    "GATEWAY_TOKEN_SECRET_ID":"openclaw/gateway-token",
    "IDENTITY_TABLE_NAME":"openclaw-identity",
    "S3_USER_FILES_BUCKET":"openclaw-user-files-<ACCOUNT_ID>-us-west-2",
    "SUBAGENT_BEDROCK_MODEL_ID":"global.anthropic.claude-opus-4-6-v1",
    "BROWSER_IDENTIFIER":"<BROWSER_ID>"
  }' \
  --region us-west-2
```

### 4. Stop Old Sessions (REQUIRED after runtime update)

Runtime updates do NOT replace running containers. You must stop old sessions manually.

```bash
# Find session ID
SID=$(aws dynamodb get-item --table-name openclaw-identity --region us-west-2 \
  --key '{"PK":{"S":"USER#<userId>"},"SK":{"S":"SESSION"}}' \
  --query 'Item.sessionId.S' --output text)

# Stop it
agentcore stop-session -a openclaw_agent -s "$SID"

# Delete DynamoDB record to force new session creation
aws dynamodb delete-item --table-name openclaw-identity --region us-west-2 \
  --key '{"PK":{"S":"USER#<userId>"},"SK":{"S":"SESSION"}}'
```

### 5. Verify

```bash
# Check runtime status
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> --region us-west-2 \
  --query '{status:status,version:agentRuntimeVersion,image:agentRuntimeArtifact.containerConfiguration.containerUri}'

# Test invoke
agentcore invoke '{"action":"status"}' -a openclaw_agent

# Test chat (new session)
agentcore invoke '{"action":"chat","userId":"test","actorId":"test:t1","channel":"test","message":"hi"}' -a openclaw_agent
```

---

## Debugging Playbook

### Log Locations

| Log | Command |
|-----|---------|
| Router Lambda | `aws logs tail /openclaw/lambda/router --region us-west-2 --since 10m` |
| Cron Lambda | `aws logs tail /openclaw/lambda/cron --region us-west-2 --since 10m` |
| Container stdout | `aws logs tail /openclaw/container --region us-west-2 --since 10m` |
| CodeBuild | Find build ID → `aws codebuild batch-get-builds --ids <id> --query 'builds[0].logs'` |

### Common Error → Root Cause → Fix

| Error Message | Root Cause | Fix |
|--------------|-----------|-----|
| `Runtime initialization time exceeded 120s` | ECR pull permissions missing — execution role can't pull from `bedrock-agentcore-*` ECR repo | Add `arn:aws:ecr:{region}:{account}:repository/bedrock-agentcore-openclaw_agent` to IAM policy |
| `The provided model identifier is invalid` | Wrong Bedrock model ID OR stale container using old env vars | 1. Verify model ID works: `aws bedrock-runtime converse --model-id <id> ...` 2. Stop old session |
| `Gateway token not available` | Container env var `GATEWAY_TOKEN_SECRET_ID` missing or secret empty | Check runtime env vars (full replace issue?) and Secrets Manager value |
| `I'm having trouble connecting right now` | Proxy started but Bedrock call failed — usually network or model access | Check VPC endpoints, NAT Gateway, Bedrock model access |
| `I'm having trouble starting up` | Init failed — proxy didn't start within 30s | Check container logs for crash reason |
| `Feishu: ignoring event type:` (empty) | Feishu event encrypted but not decrypted | Ensure `_decrypt_feishu_event()` is called before parsing |
| `Packed policy consumes N% of allotted space` | STS session policy exceeds 2048 packed bytes | Remove Condition blocks, use shorter ARNs, reduce actions |
| `NameError` / `chat_id` not defined in cron Lambda | Feishu send function has wrong variable names | Fix `send_feishu_message` in `lambda/cron/index.py` |
| `HTTP Error 400` sending Feishu cron message | Using `chat_id` (oc_) but target is `open_id` (ou_) | Use `receive_id_type=open_id` for P2P cron delivery |
| Container starts but `proxyReady: false` | Missing env vars (stale session from before runtime update) | Stop session, delete DynamoDB SESSION record |

### Diagnostic Commands

```bash
# Check container init state
agentcore invoke '{"action":"status"}' -a openclaw_agent

# Search for errors in router
aws logs filter-log-events --log-group-name /openclaw/lambda/router \
  --region us-west-2 --start-time $(python3 -c "import time; print(int((time.time()-300)*1000))") \
  --filter-pattern "ERROR" --query 'events[*].message' --output text

# Check all active sessions
aws dynamodb scan --table-name openclaw-identity --region us-west-2 \
  --filter-expression "SK = :sk" \
  --expression-attribute-values '{":sk":{"S":"SESSION"}}' \
  --query 'Items[*].{user:PK.S,session:sessionId.S}'

# Check ECR images
aws ecr describe-images --repository-name bedrock-agentcore-openclaw_agent \
  --region us-west-2 --query 'imageDetails[*].{tag:imageTags[0],size:imageSizeInBytes,pushed:imagePushedAt}' \
  --output table

# Full runtime config dump
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> --region us-west-2

# Check VPC endpoints
aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=<VPC_ID>" \
  --region us-west-2 --query 'VpcEndpoints[*].{Service:ServiceName,State:State}'

# Nuke all sessions (for clean testing)
for item in $(aws dynamodb scan --table-name openclaw-identity --region us-west-2 \
  --filter-expression "SK = :sk" --expression-attribute-values '{":sk":{"S":"SESSION"}}' \
  --query 'Items[*].[PK.S,sessionId.S]' --output text); do
  PK=$(echo "$item" | cut -f1); SID=$(echo "$item" | cut -f2)
  agentcore stop-session -a openclaw_agent -s "$SID" 2>/dev/null
  aws dynamodb delete-item --table-name openclaw-identity --region us-west-2 \
    --key "{\"PK\":{\"S\":\"$PK\"},\"SK\":{\"S\":\"SESSION\"}}"
done
```

---

## Adding a New Channel — Complete Checklist

Use this when integrating a new messaging platform (e.g., Discord, WhatsApp, LINE).

### 1. Security Stack — Add Secret

`stacks/security_stack.py`:
```python
self.newchannel_token_secret = secretsmanager.Secret(
    self, "NewChannelBotTokenSecret",
    secret_name="openclaw/channels/newchannel",
    encryption_key=self.cmk,
)
```

### 2. Router Stack — Add Environment Variable

`stacks/router_stack.py` — add to Lambda environment:
```python
"NEWCHANNEL_TOKEN_SECRET_ID": newchannel_token_secret_name,
```

### 3. Router Stack — Add API Gateway Route

`stacks/router_stack.py`:
```python
apigwv2.CfnRoute(self, "NewChannelRoute",
    api_id=self.api.api_id,
    route_key="POST /webhook/newchannel",
    target=f"integrations/{integration.ref}",
)
```

### 4. Router Lambda — Implement Handler

`lambda/router/index.py` — add these functions:

```python
# Secret fetching
NEWCHANNEL_TOKEN_SECRET_ID = os.environ.get("NEWCHANNEL_TOKEN_SECRET_ID", "")

def _get_newchannel_credentials():
    """Return credentials from Secrets Manager."""
    raw = _get_secret(NEWCHANNEL_TOKEN_SECRET_ID)
    # Parse as needed

# Webhook validation
def validate_newchannel_webhook(headers, body):
    """Validate incoming webhook signature."""
    # Implement platform-specific validation

# Message sending
def send_newchannel_message(target_id, text):
    """Send message via platform API."""
    # Implement platform-specific sending

# Progress notification (for long-running requests)
def _newchannel_progress_notify(target_id, stop_event, notify_after_s=30):
    """Send progress message if request takes too long."""
    if not stop_event.wait(timeout=notify_after_s):
        send_newchannel_message(target_id, "Working on your request...")

# Main handler
def handle_newchannel(body, headers=None):
    """Process webhook event."""
    # 1. Parse event
    # 2. Extract sender_id, message text, images
    # 3. Resolve user identity (is_user_allowed, get_or_create_user, get_or_create_session)
    # 4. Invoke AgentCore with progress notification thread
    # 5. Send response back
```

### 5. Router Lambda — Add Route Dispatch

In `handler()` function:
```python
elif path.endswith("/webhook/newchannel"):
    # Validate webhook
    if not validate_newchannel_webhook(headers, body):
        return {"statusCode": 401, "body": "Unauthorized"}
    _self_invoke_async("newchannel", body, headers)
    return {"statusCode": 200, "body": "ok"}
```

And in async dispatch:
```python
elif channel == "newchannel":
    handle_newchannel(body, headers)
```

### 6. Cron Lambda — Add Delivery Support

`lambda/cron/index.py` — add to `deliver_response()`:
```python
elif channel == "newchannel":
    send_newchannel_message(channel_target, response_text)
```

And implement `send_newchannel_message()` in the cron Lambda (it has its own copy since it runs independently).

### 7. Feishu-Specific Gotchas (reference for similar platforms)

These issues were discovered during Feishu integration and apply to similar platforms:

- **Event encryption**: Feishu encrypts webhook events with AES-256-CBC when Encrypt Key is set. Must decrypt before parsing. Use ctypes/OpenSSL (not pycryptodome) to avoid native binary architecture issues in Lambda
- **Token refresh**: Feishu `tenant_access_token` expires in 2 hours — cache with early refresh
- **P2P vs Group**: Group messages include @mention tags that must be stripped
- **Image download**: Requires `tenant_access_token` auth header
- **Cron delivery**: P2P messages use `open_id` (ou_), not `chat_id` (oc_) — `receive_id_type` must match
- **URL verification**: Platform sends a challenge on webhook setup — must respond synchronously (before async dispatch)
- **`setup-*.sh` region**: Scripts read `CDK_DEFAULT_REGION` — set it explicitly if shell default differs

### 8. Setup Script

Create `scripts/setup-newchannel.sh`:
- Display webhook URL
- Prompt for credentials → store in Secrets Manager
- Prompt for user ID → add to DynamoDB allowlist

### 9. Tests

Create `lambda/router/test_newchannel.py`:
- Webhook validation (valid/invalid signatures)
- URL verification challenge
- Message parsing (text, images, group vs P2P)
- User allowlist checking
- Error handling

### 10. Documentation

Update:
- `CLAUDE.md` — add channel to architecture, tech stack, gotchas
- `docs/design-newchannel.md` — design document with API details
- `README.md` — add to supported channels list

---

## Key IDs (us-west-2)

| Resource | Value |
|----------|-------|
| Runtime ID | `<RUNTIME_ID>` |
| Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/<RUNTIME_ID>` |
| ECR Repo | `<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent` |
| Execution Role | `openclaw-agentcore-execution-role-us-west-2` |
| VPC | `<VPC_ID>` |
| Private Subnets | `<PRIVATE_SUBNET_1>`, `<PRIVATE_SUBNET_2>` |
| Security Group | `<SECURITY_GROUP_ID>` |
| Memory ID | `<MEMORY_ID>` |
| DynamoDB Table | `openclaw-identity` |
| S3 Bucket | `openclaw-user-files-<ACCOUNT_ID>-us-west-2` |
| API Gateway | `https://10u4uqxbv2.execute-api.us-west-2.amazonaws.com/` |

## Lessons Learned

1. **`update-agent-runtime` wipes env vars if omitted** — always include full `--environment-variables`
2. **Stale sessions mask fixes** — always stop old sessions after runtime update
3. **ECR repo naming mismatch** — Starter Toolkit creates `bedrock-agentcore-*` repos; CDK IAM policies must include this pattern
4. **120s init timeout = check ECR permissions first** — misleading error message
5. **Feishu AES decryption** — use ctypes/OpenSSL, not pycryptodome (Lambda architecture mismatch)
6. **STS session policy 2048 byte packed limit** — remove Condition blocks, keep policies minimal
7. **VPC endpoint not the cause of model ID errors** — cross-region inference profiles work through VPC endpoints
8. **Container CloudWatch logs not automatic** — need cloudwatch-logger.js (custom module that hooks console.log)
9. **Warm pool unnecessary** — lightweight agent responds in ~5s; OpenClaw ready in ~5s more. Cold start is fast enough
10. **Docker Hub rate limits in CodeBuild** — use ECR Public Gallery base images (`public.ecr.aws/docker/library/node:22-slim`)
