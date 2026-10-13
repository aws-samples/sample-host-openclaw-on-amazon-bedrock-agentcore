---
name: agentcore-operations
description: "OpenClaw on AgentCore Runtime operations — deployment (CDK + pinned V2 SDK), Docker build/push, AgentCore Runtime management, session lifecycle, debugging, adding new channels, and common gotchas. Use this skill for deployment, troubleshooting, operations questions, adding new messaging channels, or when something isn't working."
user-invocable: true
---

# OpenClaw on AgentCore — Operations Guide

## V2 Deployment Architecture

CDK manages foundation and dependent stacks. Phase 2 uses the pinned SDK controller because CloudFormation/CDK currently cannot set `platformVersion`. The controller explicitly selects V2, preserves existing configuration, verifies READY/readback, and performs a billed full-OpenClaw chat warmup. Follow [the runbook](../../docs/runtime-v2.md) for setup and validation.

## Deployment Playbook

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv venv .venv-deploy
uv pip sync --python .venv-deploy/bin/python requirements-deploy.txt
./scripts/deploy.sh                    # Fresh V2 release_1
```

The default manifest is `.runtime/runtime-v2.json`; CDK context is `runtime_manifest`. Foundation-only `--phase1` needs no manifest. Dependent `--phase3` requires successful real-chat warmup and V2 readback. For changed images/configuration, preserve the previous manifest and use a new immutable endpoint:

```bash
./scripts/deploy.sh --runtime-only --endpoint-name release_2
./scripts/deploy.sh --phase3
```

Use `--image-uri "$IMAGE_URI"` for a reviewed prebuilt ARM64 image. Existing routes keep their AgentCore Runtime/endpoint/version. Never delete SESSION records, stop all users, or repoint an existing endpoint to force upgrades. A session-specific stop uses the saved ARN, endpoint name and session ID and leaves that route intact. No V1 maintenance, fallback, platform selection or enrollment opt-in flags exist.

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
| `Runtime initialization time exceeded 120s` | ECR pull permissions missing — execution role can't pull from `bedrock-agentcore-*` ECR repo | Add `arn:aws:ecr:{region}:{account}:repository/openclaw-bridge` to IAM policy |
| `The provided model identifier is invalid` | Wrong Bedrock model ID OR stale container using old env vars | 1. Verify model ID works: `aws bedrock-runtime converse --model-id <id> ...` 2. Inspect the saved route and configuration; deploy a reviewed release |
| `Gateway token not available` | Container env var `GATEWAY_TOKEN_SECRET_ID` missing or secret empty | Check AgentCore Runtime env vars (full replace issue?) and Secrets Manager value |
| `I'm having trouble connecting right now` | Proxy started but Bedrock call failed — usually network or model access | Check VPC endpoints, NAT Gateway, Bedrock model access |
| `I'm having trouble starting up` | Init failed — proxy didn't start within 30s | Check container logs for crash reason |
| `Feishu: ignoring event type:` (empty) | Feishu event encrypted but not decrypted | Ensure `_decrypt_feishu_event()` is called before parsing |
| `Packed policy consumes N% of allotted space` | STS session policy exceeds 2048 packed bytes | Remove Condition blocks, use shorter ARNs, reduce actions |
| `NameError` / `chat_id` not defined in cron Lambda | Feishu send function has wrong variable names | Fix `send_feishu_message` in `lambda/cron/index.py` |
| `HTTP Error 400` sending Feishu cron message | Using `chat_id` (oc_) but target is `open_id` (ou_) | Use `receive_id_type=open_id` for P2P cron delivery |
| Container starts but `proxyReady: false` | Missing env vars (stale session from before AgentCore Runtime update) | Inspect strict restore and readiness for the saved V2 binding; retain the route |

### Diagnostic Commands

```bash
# Verify the deployment target; user diagnostics must use the saved SESSION route
uv run --no-project --python .venv-deploy/bin/python python scripts/runtime_deploy.py verify \
  --region "$CDK_DEFAULT_REGION" --manifest .runtime/runtime-v2.json --require-enrollment-ready

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
aws ecr describe-images --repository-name openclaw-bridge \
  --region us-west-2 --query 'imageDetails[*].{tag:imageTags[0],size:imageSizeInBytes,pushed:imagePushedAt}' \
  --output table

# Full runtime config dump
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> --region us-west-2

# Check VPC endpoints
aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=<VPC_ID>" \
  --region us-west-2 --query 'VpcEndpoints[*].{Service:ServiceName,State:State}'

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
| AgentCore Runtime ID | `<RUNTIME_ID>` |
| AgentCore Runtime ARN | `arn:aws:bedrock-agentcore:us-west-2:<ACCOUNT_ID>:runtime/<RUNTIME_ID>` |
| ECR Repo | `<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/openclaw-bridge` |
| Execution Role | `openclaw-agentcore-execution-role-us-west-2` |
| VPC | `<VPC_ID>` |
| Private Subnets | `<PRIVATE_SUBNET_1>`, `<PRIVATE_SUBNET_2>` |
| Security Group | `<SECURITY_GROUP_ID>` |
| Memory ID | `<MEMORY_ID>` |
| DynamoDB Table | `openclaw-identity` |
| S3 Bucket | `openclaw-user-files-<ACCOUNT_ID>-us-west-2` |
| API Gateway | `https://10u4uqxbv2.execute-api.us-west-2.amazonaws.com/` |

## Lessons Learned

1. **Configuration preservation** — use the pinned SDK controller and inspect its readback.
2. **Saved routes stay pinned** — stopping a session does not change its release endpoint.
3. **ECR repo naming mismatch** — execution-role pull permissions must cover the repository selected by the build helper.
4. **120s init timeout = check ECR permissions first** — misleading error message
5. **Feishu AES decryption** — use ctypes/OpenSSL, not pycryptodome (Lambda architecture mismatch)
6. **STS session policy 2048 byte packed limit** — remove Condition blocks, keep policies minimal
7. **VPC endpoint not the cause of model ID errors** — cross-region inference profiles work through VPC endpoints
8. **Container CloudWatch logs not automatic** — need cloudwatch-logger.js (custom module that hooks console.log)
9. **Startup evidence** — historical status measurements do not prove current-image chat or USER_READY latency.
10. **Docker Hub rate limits in CodeBuild** — use ECR Public Gallery base images (`public.ecr.aws/docker/library/node:22-slim`)
