# Deployment Issues and Fixes

This document describes issues encountered during deployment and their solutions.

## Issue 1: IAM Circular Dependency Error

### Problem

During `cdk deploy`, the OpenClawAgentCore stack fails with:

```
CREATE_FAILED: Resource handler returned message: "Invalid principal in policy: 
"AWS":"arn:aws:iam::365869126441:role/openclaw-agentcore-execution-role" 
(Service: Iam, Status Code: 400, Request ID: ...) (SDK Attempt Count: 1)" 
(RequestToken: ..., HandlerErrorCode: InvalidRequest)
```

### Root Cause

The `OpenClawExecutionRole` in `stacks/agentcore_stack.py` attempts to reference itself in the trust policy during role creation. IAM does not allow a role to reference its own ARN in the trust policy at creation time, even when using a deterministic ARN string.

The problematic code:

```python
execution_role_arn_str = f"arn:aws:iam::{account}:role/{execution_role_name}"
...
self.execution_role.assume_role_policy.add_statements(
    iam.PolicyStatement(
        actions=["sts:AssumeRole"],
        principals=[iam.ArnPrincipal(execution_role_arn_str)],  # ❌ Fails
        conditions={"StringLike": {"sts:RoleSessionName": "scoped-*"}},
    )
)
```

### Why This Pattern Was Used

The self-assume capability is required for **scoped S3 credentials**. The AgentCore container needs to:

1. Assume its own role with a session policy
2. Restrict S3 access to `s3://bucket/users/{user_id}/*` per user
3. Prevent cross-user file access

This is a security feature to ensure workspace isolation between users.

### Solution

**Modified File**: `stacks/agentcore_stack.py`

1. Changed inline policy resource from hardcoded ARN to `self.execution_role.role_arn`
2. Commented out the trust policy self-reference
3. Added comprehensive documentation for manual post-deployment step

**Post-Deployment Manual Step Required**:

After `cdk deploy` succeeds, run:

```bash
export AWS_REGION=ap-southeast-2
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws iam update-assume-role-policy \
  --role-name openclaw-agentcore-execution-role \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": [
            "ecs-tasks.amazonaws.com",
            "bedrock.amazonaws.com",
            "bedrock-agentcore.amazonaws.com"
          ]
        },
        "Action": "sts:AssumeRole"
      },
      {
        "Effect": "Allow",
        "Principal": {
          "AWS": "arn:aws:iam::'$AWS_ACCOUNT':role/openclaw-agentcore-execution-role"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringLike": {
            "sts:RoleSessionName": "scoped-*"
          }
        }
      }
    ]
  }' \
  --region $AWS_REGION
```

This allows the container to call `sts:AssumeRole` on itself with a restricted session policy.

### Alternative Solutions Considered

1. **Custom Resource**: Use a Lambda-backed custom resource to update the trust policy after role creation
   - **Pros**: Fully automated
   - **Cons**: More complex, additional Lambda + IAM permissions
   
2. **Split into Two Stacks**: Create role in one stack, reference in another
   - **Pros**: CDK-native solution
   - **Cons**: Complicates stack dependencies

3. **IAM Policy Instead of Trust Policy**: Use resource-based policies on S3
   - **Pros**: Avoids circular dependency
   - **Cons**: Requires bucket policy updates per user, less secure

The manual step approach was chosen for:
- Simplicity and transparency
- No additional infrastructure
- Clear documentation of the security requirement
- Easy to verify and troubleshoot

---

## Issue 2: ClawHub Rate Limit During Docker Build

### Problem

Docker build fails at the ClawHub skill installation step:

```
#10 6.103 ✖ Rate limit exceeded
#10 6.103 Error: Rate limit exceeded
#10 ERROR: process "/bin/sh -c clawhub install jina-reader --no-input --force && ..." 
did not complete successfully: exit code: 1
```

### Root Cause

The `clawhub` CLI queries the ClawHub marketplace API during `clawhub install`. Automated builds trigger API rate limits, especially:

- Multiple builds in quick succession
- Parallel builds (e.g., CI/CD pipelines)
- Shared IP addresses (e.g., cloud build environments)

### Solution

**Modified File**: `bridge/Dockerfile`

1. Removed `clawhub@latest` from global npm install
2. Commented out all `clawhub install` commands
3. Added documentation for post-deployment skill installation

**Runtime Skill Installation**:

Users can install skills after the container is running:

```bash
# Inside the AgentCore container
clawhub install jina-reader
clawhub install deep-research-pro
clawhub install telegram-compose
clawhub install transcript
clawhub install task-decomposer
```

**Pros**:
- Build always succeeds
- Users choose which skills they need
- Reduces image size
- Avoids rate limit issues

**Cons**:
- Skills not available immediately
- Requires manual installation step

### Future Enhancement

For teams that need pre-installed skills:

1. **Build your own base image** with skills pre-installed
2. **Use a private ClawHub API key** (if available) with higher rate limits
3. **Cache skill installations** in a separate Docker layer
4. **Copy skills from a local directory** instead of downloading

Example:

```dockerfile
# Copy pre-downloaded skills
COPY ./skills-cache/ /skills/
```

---

## Testing the Fixes

### 1. Verify CDK Deployment

```bash
cd sample-host-openclaw-on-amazon-bedrock-agentcore
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=ap-southeast-2

# Bootstrap (if not already done)
cdk bootstrap aws://$CDK_DEFAULT_ACCOUNT/$CDK_DEFAULT_REGION

# Deploy all stacks
cdk deploy --all --require-approval never
```

Expected result: All 7 stacks deploy successfully (OpenClawAgentCore no longer fails).

### 2. Build Docker Image

```bash
VERSION=$(python3 -c "import json; print(json.load(open('cdk.json'))['context']['image_version'])")

# Build ARM64 image
docker build --platform linux/arm64 -t openclaw-bridge:v${VERSION} bridge/
```

Expected result: Build completes without ClawHub rate limit errors.

### 3. Apply IAM Trust Policy Fix

```bash
export AWS_REGION=ap-southeast-2
export AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

aws iam update-assume-role-policy \
  --role-name openclaw-agentcore-execution-role \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Service": ["ecs-tasks.amazonaws.com","bedrock.amazonaws.com","bedrock-agentcore.amazonaws.com"]
        },
        "Action": "sts:AssumeRole"
      },
      {
        "Effect": "Allow",
        "Principal": {
          "AWS": "arn:aws:iam::'$AWS_ACCOUNT':role/openclaw-agentcore-execution-role"
        },
        "Action": "sts:AssumeRole",
        "Condition": {
          "StringLike": {
            "sts:RoleSessionName": "scoped-*"
          }
        }
      }
    ]
  }' \
  --region $AWS_REGION
```

### 4. Verify Deployment

```bash
# Check API Gateway health
curl https://<API_ID>.execute-api.ap-southeast-2.amazonaws.com/health

# Expected output:
# {"status":"ok","service":"openclaw-router"}
```

---

## Deployment Timeline

Based on actual deployment (2026-03-05):

| Time (UTC) | Event |
|------------|-------|
| 08:09 | Started CDK deployment |
| 08:12 | CDK Bootstrap completed |
| 08:18 | **OpenClawAgentCore failed** (IAM circular dependency) |
| 08:56 | Fixed `agentcore_stack.py` |
| 09:04 | OpenClawAgentCore deployed successfully |
| 09:09 | All 7 stacks completed |
| 09:15 | **Docker build failed** (ClawHub rate limit) |
| 09:16 | Fixed `Dockerfile` |
| 09:19 | Docker image built successfully |
| 09:20 | Image pushed to ECR |
| 09:22 | Deployment complete |

**Total time**: ~1 hour 15 minutes (including troubleshooting)

---

## Impact Assessment

### Changes Made

1. **IAM Role Trust Policy**: Requires manual post-deployment step
2. **Docker Image**: No pre-installed ClawHub skills

### Functionality Impact

- ✅ Core OpenClaw functionality: **No impact**
- ✅ Bedrock integration: **No impact**
- ✅ S3 workspace sync: **No impact**
- ⚠️ Scoped S3 credentials: **Requires manual IAM update**
- ⚠️ ClawHub skills: **Not pre-installed, install at runtime**

### Security Considerations

- The IAM trust policy update is **security-critical** for multi-user deployments
- Without it, all users share the same S3 permissions (potential data leakage)
- Single-user deployments can skip this step safely

---

## Questions?

For issues or questions:

1. Check CloudFormation stack events: `aws cloudformation describe-stack-events --stack-name OpenClawAgentCore`
2. Review Lambda logs: `aws logs tail /aws/lambda/openclaw-router --follow`
3. Open an issue: https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore/issues
