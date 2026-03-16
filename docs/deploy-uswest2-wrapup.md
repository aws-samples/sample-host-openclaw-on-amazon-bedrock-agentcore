# us-west-2 Deployment Wrap-up (2026-03-11)

## Branch

`deploy/starter-toolkit-hybrid` (worktree at `~/g-repo/openclaw-deploy`)

Merged `research/feishu-channel-integration` (Feishu channel Phase 1) into this branch.

## What was accomplished

### Infrastructure (CDK stacks deployed successfully)

All 7 CDK stacks deployed to us-west-2:

| Stack | Status |
|---|---|
| OpenClawVpc | Deployed |
| OpenClawSecurity | Deployed |
| OpenClawAgentCore | Deployed (ECR repo, S3 bucket, SG, IAM role) |
| OpenClawRouter | Deployed (Lambda + API Gateway + DynamoDB) |
| OpenClawCron | Deployed |
| OpenClawObservability | Deployed |
| OpenClawTokenMonitoring | Deployed |

### AgentCore Runtime (Starter Toolkit)

- Runtime created: `<RUNTIME_ID>`
- Endpoint: `DEFAULT`
- Docker image built locally and pushed: `bedrock-agentcore-openclaw_agent:local-build`
- Memory: `<MEMORY_ID>` (STM only)

### Feishu channel

- Webhook URL configured: `https://<API_GATEWAY_ID>.execute-api.us-west-2.amazonaws.com/webhook/feishu`
- Credentials stored in Secrets Manager
- User allowlisted in DynamoDB
- AES-256-CBC decryption added (pure ctypes/OpenSSL, zero dependencies)
- Decryption verified working in Lambda logs

### Old stacks cleaned up

- `serverless-ecs-image-handler-stack` (UPDATE_ROLLBACK_FAILED) — deleted
- `winserver7` (contained mcc-seller-vpc) — deleted

## Outstanding issues

### 1. Proxy not starting inside container (BLOCKING)

**Symptom:** `proxyReady: false`, user gets "I'm having trouble connecting right now."

**What we know:**
- Container starts and responds to `/ping` in ~2s (verified locally)
- `secretsReady: true` — Secrets Manager fetch works
- `init()` is triggered on chat action, proxy process is spawned
- Proxy never becomes ready on port 18790
- No runtime log group exists (`/aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT`) — container stdout not captured

**Likely cause:** Proxy needs outbound network to reach Bedrock/Cognito. Private subnets have NAT + VPC endpoints (`bedrock-runtime`, `ecr.api`, `ecr.dkr`, `secretsmanager`, `logs`, `monitoring`, `ssm`, `s3`). Security group egress only allows TCP 443 — should be sufficient but needs verification from container logs.

**Next step:** Get container stdout logs. Either:
- Fix observability config so runtime logs appear in CloudWatch
- Or add diagnostic logging to `/invocations` status response (e.g., proxy exit code, stderr capture)

### 2. No runtime CloudWatch logs

The log group `/aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT` was never created. Other agents in the same account have this log group. May be a Starter Toolkit observability setup gap vs CDK's `CfnRuntime` approach.

## Root cause found & fixed: ECR permissions

The 120s initialization timeout was caused by **IAM ECR permissions mismatch**:

- CDK created the execution role with ECR resource pattern: `openclaw-bridge-*` and `openclaw_agent*`
- Starter Toolkit created ECR repo named: `bedrock-agentcore-openclaw_agent`
- Pattern `openclaw_agent*` does NOT match `bedrock-agentcore-openclaw_agent`
- Fix: Added `arn:aws:ecr:us-west-2:<ACCOUNT_ID>:repository/bedrock-agentcore-openclaw_agent` to the inline policy

**This fix was applied directly via `aws iam put-role-policy` and needs to be reflected in CDK code.**

## Key commands reference

### CDK

```bash
# Activate venv
cd ~/g-repo/openclaw-deploy && source .venv/bin/activate

# Synth (validate all 7 stacks)
export CDK_DEFAULT_ACCOUNT=<ACCOUNT_ID> CDK_DEFAULT_REGION=us-west-2
cdk synth

# Deploy all
cdk deploy --all --require-approval never

# Deploy single stack
cdk deploy OpenClawRouter --require-approval never

# Deploy single stack without dependencies
cdk deploy OpenClawRouter --require-approval never --exclusively
```

### Starter Toolkit

```bash
# Install
pip3 install --break-system-packages bedrock-agentcore-starter-toolkit

# Configure agent
~/.local/bin/agentcore configure -n openclaw_agent -e bridge/agentcore-contract.js \
  --vpc --subnets <PRIVATE_SUBNET_1>,<PRIVATE_SUBNET_2> \
  --security-groups <SECURITY_GROUP_ID> -ni -r us-west-2

# Deploy (CodeBuild — default, no local Docker needed)
~/.local/bin/agentcore deploy -a openclaw_agent --auto-update-on-conflict

# Deploy (local build — skip CodeBuild, use pre-pushed image)
~/.local/bin/agentcore deploy -a openclaw_agent --local-build --image-tag local-build --auto-update-on-conflict

# Deploy with env vars
~/.local/bin/agentcore deploy -a openclaw_agent --local-build --image-tag local-build \
  --auto-update-on-conflict \
  --env "EXECUTION_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/openclaw-agentcore-execution-role-us-west-2" \
  --env "BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514" \
  # ... (see full list below)

# Status
~/.local/bin/agentcore status -a openclaw_agent -v

# Invoke (test)
~/.local/bin/agentcore invoke '{"prompt": "hi"}' -a openclaw_agent
~/.local/bin/agentcore invoke '{"action":"status"}' -a openclaw_agent
~/.local/bin/agentcore invoke '{"action":"chat","userId":"test","actorId":"test:123","channel":"test","message":"hello"}' -a openclaw_agent
```

### Direct AWS API (bypass Starter Toolkit limitations)

```bash
# Update runtime — change subnets, image, env vars
aws bedrock-agentcore-control update-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> \
  --role-arn "arn:aws:iam::<ACCOUNT_ID>:role/openclaw-agentcore-execution-role-us-west-2" \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent:local-build"}}' \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"subnets":["<PRIVATE_SUBNET_1>","<PRIVATE_SUBNET_2>"],"securityGroups":["<SECURITY_GROUP_ID>"]}}' \
  --environment-variables '{...}' \
  --region us-west-2

# Get runtime config
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <RUNTIME_ID> --region us-west-2

# List endpoints
aws bedrock-agentcore-control list-agent-runtime-endpoints \
  --agent-runtime-id <RUNTIME_ID> --region us-west-2
```

### Docker (local build & push)

```bash
# Build ARM64 image locally
cd ~/g-repo/openclaw-deploy/bridge
sudo docker build --platform linux/arm64 -t openclaw-bridge:usw2 .

# Test locally
sudo docker run --rm -d --name test -p 8080:8080 -e AWS_REGION=us-west-2 openclaw-bridge:usw2
curl http://localhost:8080/ping
sudo docker stop test

# Login to ECR
aws ecr get-login-password --region us-west-2 | sudo docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com

# Tag and push
sudo docker tag openclaw-bridge:usw2 <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent:local-build
sudo docker push <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent:local-build

# Cross-region image copy
sudo docker pull --platform linux/arm64 <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/openclaw-bridge:latest
sudo docker tag <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/openclaw-bridge:latest \
  <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent:from-east1
sudo docker push <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/bedrock-agentcore-openclaw_agent:from-east1
```

### Feishu setup

```bash
CDK_DEFAULT_REGION=us-west-2 bash ~/g-repo/openclaw-deploy/scripts/setup-feishu.sh
```

### Debugging

```bash
# Router Lambda logs
aws logs filter-log-events --log-group-name /openclaw/lambda/router --region us-west-2 \
  --start-time $(python3 -c "import time; print(int((time.time()-300)*1000))") \
  --filter-pattern "ERROR" --query 'events[*].message' --output text

# Latest router log stream
STREAM=$(aws logs describe-log-streams --log-group-name /openclaw/lambda/router --region us-west-2 \
  --order-by LastEventTime --descending --limit 1 --query 'logStreams[0].logStreamName' --output text)
aws logs get-log-events --log-group-name /openclaw/lambda/router --log-stream-name "$STREAM" \
  --region us-west-2 --query 'events[*].message' --output text

# CodeBuild logs
BUILD_ID="bedrock-agentcore-openclaw_agent-builder:<build-uuid>"
aws codebuild batch-get-builds --ids "$BUILD_ID" --region us-west-2 --query 'builds[0].logs'

# ECR images
aws ecr describe-images --repository-name bedrock-agentcore-openclaw_agent --region us-west-2 \
  --query 'imageDetails[*].{tag:imageTags[0],size:imageSizeInBytes,pushed:imagePushedAt}' --output table

# Check IAM role policies
aws iam get-role-policy --role-name openclaw-agentcore-execution-role-us-west-2 \
  --policy-name OpenClawExecutionRoleDefaultPolicy466AD231
```

## Required environment variables for runtime

These must be passed via `--env` flags or `update-agent-runtime --environment-variables`:

```
EXECUTION_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/openclaw-agentcore-execution-role-us-west-2
EVENTBRIDGE_ROLE_ARN=arn:aws:iam::<ACCOUNT_ID>:role/openclaw-cron-scheduler-role-us-west-2
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514
S3_USER_FILES_BUCKET=openclaw-user-files-<ACCOUNT_ID>-us-west-2
SUBAGENT_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514
GATEWAY_TOKEN_SECRET_ID=openclaw/gateway-token
COGNITO_USER_POOL_ID=<COGNITO_USER_POOL_ID>
COGNITO_CLIENT_ID=<COGNITO_CLIENT_ID>
COGNITO_PASSWORD_SECRET_ID=openclaw/cognito-password-secret
IDENTITY_TABLE_NAME=openclaw-identity
EVENTBRIDGE_SCHEDULE_GROUP=openclaw-cron
CRON_LEAD_TIME_MINUTES=5
BEDROCK_AGENTCORE_MEMORY_ID=<MEMORY_ID>
BEDROCK_AGENTCORE_MEMORY_NAME=openclaw_agent_mem
```

## Lessons learned

1. **Starter Toolkit creates ECR repo with prefix `bedrock-agentcore-`** — CDK IAM policies must include this naming pattern
2. **VPC subnet changes are immutable** via Starter Toolkit config, but **mutable via direct `update-agent-runtime` API**
3. **Starter Toolkit `--local-build` skips CodeBuild** — useful for pre-pushed images
4. **CodeBuild default mode always rebuilds** — even with `--image-tag`, it runs a fresh Docker build that overwrites the tag
5. **Feishu events are AES-256-CBC encrypted** when Encrypt Key is set — Lambda needs decryption before parsing
6. **pycryptodome native binaries are architecture-specific** — CDK bundling builds for ARM64 but Lambda runs x86_64; use ctypes/OpenSSL instead
7. **`setup-feishu.sh` reads `CDK_DEFAULT_REGION`** — must be set explicitly if shell default differs
8. **AgentCore "initialization exceeded 120s" can mean ECR permission denied** — the error message is misleading; check IAM ECR permissions first

## us-east-1 vs us-west-2 comparison

| Aspect | us-east-1 (working) | us-west-2 (in progress) |
|---|---|---|
| Deployment | Pure CDK (`CfnRuntime`) | CDK + Starter Toolkit |
| ECR repo name | `openclaw-bridge` | `bedrock-agentcore-openclaw_agent` |
| VPC subnets | Default VPC (public) | Custom VPC (private + NAT) |
| Runtime logs | Present | Missing (no log group) |
| Image | 514 MB (local cross-compile) | 514 MB (local cross-compile) |
| Proxy status | Working | Not starting (needs debugging) |
