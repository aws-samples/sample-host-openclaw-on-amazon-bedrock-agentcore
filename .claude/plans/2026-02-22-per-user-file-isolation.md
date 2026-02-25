# Per-User File Persistence Isolation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Isolate all persistent file storage (MEMORY.md, IDENTITY.md, etc.) per user so data written by one user on one channel is never visible to other users on the same or different channels.

**Architecture:** Replace OpenClaw's shared local filesystem persistence with an S3-backed per-user file skill. The proxy injects `actorId` + isolation instructions into the system prompt so Claude uses the S3 skill (namespaced by user) instead of local files. Remove the redundant `openclaw-mem` ClawHub skill since AgentCore Memory already provides per-user isolated semantic memory.

**Tech Stack:** AWS CDK (Python), S3, Node.js (OpenClaw skill scripts), `@aws-sdk/client-s3`, AgentCore Memory (existing)

---

## Problem Analysis

### Root Cause

OpenClaw runs as a **single container** serving all users across Telegram, Discord, and Slack. Three persistence mechanisms exist, but only one is per-user isolated:

| Mechanism | Storage | Per-User Isolated? |
|---|---|---|
| AgentCore Memory | Server-side (AWS) | YES — namespaced by `actorId` |
| Built-in file tools (`tools.profile: "full"`) | Container filesystem (shared `/root/.openclaw/` workspace) | **NO** — all users share one workspace |
| `openclaw-mem` skill (ClawHub) | Shared SQLite + FTS5 | **NO** — single database for all users |

When Claude wants to persist information (bot identity, notes, preferences), it uses the built-in file tools to write files like `MEMORY.md`, `IDENTITY.md` to the shared workspace. These files are visible to ALL users. The `openclaw-mem` skill similarly stores data in a shared SQLite database.

### Example Failure

1. User A (Telegram) tells the bot: "Your name is Telebot"
2. Claude writes `IDENTITY.md` with `name: Telebot` to shared workspace
3. User B (Slack) asks: "What's your name?"
4. Claude reads the SAME `IDENTITY.md` and responds "I'm Telebot"
5. User B should not see User A's customization

### What Already Works

- `agentcore-proxy.js` correctly extracts `actorId` from message envelopes (e.g., `telegram:12345`, `slack:U0123456789`)
- AgentCore Memory retrieval/storage is namespaced by `actorId.replace(/:/g, "_")`
- Memory extraction (semantic, user_preference, summary) processes events per namespace
- Cognito identity auto-provisioning works per user

---

## Solution Architecture

```
                     Claude (Bedrock)
                    /       |        \
   [System Prompt]    [Tool Calls]    [Memory Context]
   - actorId injected  |              - Retrieved per-user
   - Isolation rules   |              - from AgentCore Memory
                       v
              s3-user-files skill
              (custom OpenClaw skill)
                       |
                       v
              S3 Bucket (per-user)
              /{actorId}/IDENTITY.md
              /{actorId}/MEMORY.md
              /{actorId}/NOTES.md
              ...
```

Three-pronged approach:

1. **S3-backed per-user file skill** — New custom OpenClaw skill providing file CRUD operations, namespaced by `actorId` in S3 key prefixes
2. **System prompt enhancement** — Proxy injects `actorId`, channel, and isolation instructions into every request so Claude knows WHO it's talking to and HOW to persist data
3. **Configuration cleanup** — Remove `openclaw-mem` (redundant, shared), add S3 bucket + IAM + env vars

---

## Task Breakdown

### Task 1: Add S3 Bucket to CDK Stack

**Files:**
- Modify: `stacks/agentcore_stack.py`
- Modify: `cdk.json` (add `user_files_ttl_days` context parameter)

**Step 1: Add S3 import and bucket to `stacks/agentcore_stack.py`**

Add `aws_s3` and `Duration` to the imports at line 10-18:

```python
from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_s3 as s3,
)
```

Add the S3 bucket after the Memory section (after line 210), before WorkloadIdentity:

```python
        # --- S3 Bucket for Per-User File Storage ------------------------------
        user_files_ttl_days = int(self.node.try_get_context("user_files_ttl_days") or "365")
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"openclaw-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=None,  # Uses aws/s3 managed key; swap to cmk_arn for CMK
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-user-files",
                    expiration=Duration.days(user_files_ttl_days),
                ),
            ],
            enforce_ssl=True,
            versioned=False,
        )
```

**Step 2: Add IAM permissions for the execution role**

Add after the existing AgentCore Memory IAM block (after line 145):

```python
        # S3 per-user file storage
        self.user_files_bucket.grant_read_write(self.execution_role)
```

**Step 3: Add S3 bucket name to runtime environment variables**

In the `environment_variables` dict of `CfnRuntime` (around line 240), add:

```python
                "S3_USER_FILES_BUCKET": self.user_files_bucket.bucket_name,
```

**Step 4: Add CfnOutput for the bucket**

After the existing outputs (around line 280):

```python
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
```

**Step 5: Add cdk-nag suppression for S3**

```python
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_files_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not required for user file storage — "
                    "CloudTrail S3 data events provide sufficient audit trail.",
                ),
            ],
        )
```

**Step 6: Add context parameter to `cdk.json`**

```json
"user_files_ttl_days": 365
```

**Step 7: Verify CDK synth**

Run: `cd /home/ec2-user/projects/openclaw-on-agentcore && source .venv/bin/activate && cdk synth 2>&1 | tail -20`
Expected: No errors, stack synthesizes with new S3 bucket resource.

**Step 8: Commit**

```bash
git add stacks/agentcore_stack.py cdk.json
git commit -m "feat: add S3 bucket for per-user file storage"
```

---

### Task 2: Create S3 User Files Skill

**Files:**
- Create: `bridge/skills/s3-user-files/SKILL.md`
- Create: `bridge/skills/s3-user-files/read.js`
- Create: `bridge/skills/s3-user-files/write.js`
- Create: `bridge/skills/s3-user-files/list.js`
- Create: `bridge/skills/s3-user-files/delete.js`
- Create: `bridge/skills/s3-user-files/package.json`

**Step 1: Create SKILL.md**

Create `bridge/skills/s3-user-files/SKILL.md`:

```markdown
# S3 User Files

Per-user persistent file storage backed by AWS S3. Each user's files are stored in an isolated namespace — no cross-user data leakage.

## What This Skill Does

- Read and write text files isolated per user
- Each user has a private S3 namespace based on their user_id
- Files persist across container restarts
- Supports: read, write, list, and delete operations

## Prerequisites

- `S3_USER_FILES_BUCKET` environment variable must be set
- AWS IAM permissions for S3 read/write on the bucket
- `user_id` is provided in the system prompt as "Current user ID"

## Important

**Always use the user_id from the system prompt** when calling these tools.
Never hardcode or guess a user_id. The system provides it automatically.

## Usage

### read_user_file

Read a file from the user's persistent storage.

node /skills/s3-user-files/read.js <user_id> <filename>

- `user_id` (required): The user's unique identifier (e.g., `telegram_12345`)
- `filename` (required): The file name to read (e.g., `IDENTITY.md`)

### write_user_file

Write content to a file in the user's persistent storage.

node /skills/s3-user-files/write.js <user_id> <filename> <content>

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name to write
- `content` (required): The text content to write

### list_user_files

List all files in the user's persistent storage.

node /skills/s3-user-files/list.js <user_id>

- `user_id` (required): The user's unique identifier

### delete_user_file

Delete a file from the user's persistent storage.

node /skills/s3-user-files/delete.js <user_id> <filename>

- `user_id` (required): The user's unique identifier
- `filename` (required): The file name to delete

## From Agent Chat

- "Save my preferences" -> write_user_file with the user's preferences
- "What do you remember about me?" -> read_user_file to check stored notes
- "What's your name?" -> read_user_file IDENTITY.md for this user
- "Forget everything about me" -> delete_user_file on each stored file
- "What files do you have for me?" -> list_user_files

## Files

| File | Purpose |
|------|---------|
| `read.js` | Read a file from user's S3 namespace |
| `write.js` | Write content to user's S3 namespace |
| `list.js` | List files in user's S3 namespace |
| `delete.js` | Delete a file from user's S3 namespace |

## Security Notes

- Files are isolated per user via S3 key prefix: `{user_id}/{filename}`
- user_id uses underscores (e.g., `telegram_12345`) — colons are replaced
- Content encrypted at rest via S3 server-side encryption
- Bucket enforces SSL-only access
- Never use `default_user` as user_id — that indicates identity extraction failure
```

**Step 2: Create `package.json`**

Create `bridge/skills/s3-user-files/package.json`:

```json
{
  "name": "s3-user-files",
  "version": "1.0.0",
  "description": "Per-user persistent file storage backed by AWS S3",
  "private": true,
  "dependencies": {
    "@aws-sdk/client-s3": "^3.0.0"
  }
}
```

**Step 3: Create `write.js`**

Create `bridge/skills/s3-user-files/write.js`:

```javascript
#!/usr/bin/env node
/**
 * write_user_file — Write content to a user's S3-namespaced file.
 * Usage: node write.js <user_id> <filename> <content>
 */
const { S3Client, PutObjectCommand } = require("@aws-sdk/client-s3");

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const REGION = process.env.AWS_REGION || "us-west-2";

function sanitize(str) {
  return str.replace(/\.\./g, "").replace(/[^a-zA-Z0-9_\-./]/g, "_").slice(0, 256);
}

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];
  const content = process.argv.slice(4).join(" ");

  if (!userId || !filename || !content) {
    console.error("Usage: node write.js <user_id> <filename> <content>");
    process.exit(1);
  }

  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable not set");
    process.exit(1);
  }

  if (userId === "default-user" || userId === "default_user") {
    console.error("Error: Cannot write files for default-user. User identity not resolved.");
    process.exit(1);
  }

  const key = `${sanitize(userId)}/${sanitize(filename)}`;
  const client = new S3Client({ region: REGION });

  await client.send(new PutObjectCommand({
    Bucket: BUCKET,
    Key: key,
    Body: content,
    ContentType: "text/plain; charset=utf-8",
  }));

  console.log(`File written: ${key} (${content.length} bytes)`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
```

**Step 4: Create `read.js`**

Create `bridge/skills/s3-user-files/read.js`:

```javascript
#!/usr/bin/env node
/**
 * read_user_file — Read a file from a user's S3-namespaced storage.
 * Usage: node read.js <user_id> <filename>
 */
const { S3Client, GetObjectCommand } = require("@aws-sdk/client-s3");

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const REGION = process.env.AWS_REGION || "us-west-2";

function sanitize(str) {
  return str.replace(/\.\./g, "").replace(/[^a-zA-Z0-9_\-./]/g, "_").slice(0, 256);
}

async function streamToString(stream) {
  const chunks = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf-8");
}

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];

  if (!userId || !filename) {
    console.error("Usage: node read.js <user_id> <filename>");
    process.exit(1);
  }

  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable not set");
    process.exit(1);
  }

  const key = `${sanitize(userId)}/${sanitize(filename)}`;
  const client = new S3Client({ region: REGION });

  try {
    const response = await client.send(new GetObjectCommand({
      Bucket: BUCKET,
      Key: key,
    }));
    const content = await streamToString(response.Body);
    console.log(content);
  } catch (err) {
    if (err.name === "NoSuchKey" || err.$metadata?.httpStatusCode === 404) {
      console.log(`File not found: ${key}`);
    } else {
      throw err;
    }
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
```

**Step 5: Create `list.js`**

Create `bridge/skills/s3-user-files/list.js`:

```javascript
#!/usr/bin/env node
/**
 * list_user_files — List files in a user's S3-namespaced storage.
 * Usage: node list.js <user_id>
 */
const { S3Client, ListObjectsV2Command } = require("@aws-sdk/client-s3");

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const REGION = process.env.AWS_REGION || "us-west-2";

function sanitize(str) {
  return str.replace(/\.\./g, "").replace(/[^a-zA-Z0-9_\-./]/g, "_").slice(0, 256);
}

async function main() {
  const userId = process.argv[2];

  if (!userId) {
    console.error("Usage: node list.js <user_id>");
    process.exit(1);
  }

  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable not set");
    process.exit(1);
  }

  const prefix = `${sanitize(userId)}/`;
  const client = new S3Client({ region: REGION });

  const response = await client.send(new ListObjectsV2Command({
    Bucket: BUCKET,
    Prefix: prefix,
  }));

  const files = (response.Contents || []).map((obj) => {
    const name = obj.Key.replace(prefix, "");
    const sizeKB = (obj.Size / 1024).toFixed(1);
    const modified = obj.LastModified.toISOString().split("T")[0];
    return `- ${name} (${sizeKB} KB, ${modified})`;
  });

  if (files.length === 0) {
    console.log("No files stored for this user.");
  } else {
    console.log(`Files for ${userId}:\n${files.join("\n")}`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
```

**Step 6: Create `delete.js`**

Create `bridge/skills/s3-user-files/delete.js`:

```javascript
#!/usr/bin/env node
/**
 * delete_user_file — Delete a file from a user's S3-namespaced storage.
 * Usage: node delete.js <user_id> <filename>
 */
const { S3Client, DeleteObjectCommand } = require("@aws-sdk/client-s3");

const BUCKET = process.env.S3_USER_FILES_BUCKET;
const REGION = process.env.AWS_REGION || "us-west-2";

function sanitize(str) {
  return str.replace(/\.\./g, "").replace(/[^a-zA-Z0-9_\-./]/g, "_").slice(0, 256);
}

async function main() {
  const userId = process.argv[2];
  const filename = process.argv[3];

  if (!userId || !filename) {
    console.error("Usage: node delete.js <user_id> <filename>");
    process.exit(1);
  }

  if (!BUCKET) {
    console.error("Error: S3_USER_FILES_BUCKET environment variable not set");
    process.exit(1);
  }

  const key = `${sanitize(userId)}/${sanitize(filename)}`;
  const client = new S3Client({ region: REGION });

  await client.send(new DeleteObjectCommand({
    Bucket: BUCKET,
    Key: key,
  }));

  console.log(`File deleted: ${key}`);
}

main().catch((err) => {
  console.error(`Error: ${err.message}`);
  process.exit(1);
});
```

**Step 7: Commit**

```bash
git add bridge/skills/s3-user-files/
git commit -m "feat: add S3 per-user file storage skill for OpenClaw"
```

---

### Task 3: Enhance System Prompt with User Identity and Isolation Rules

**Files:**
- Modify: `bridge/agentcore-proxy.js`

**Step 1: Add user identity context builder function**

Add after the `retrieveMemoryContext` function (after line 195):

```javascript
/**
 * Build user identity context to inject into the system prompt.
 * Includes actorId, channel, and per-user isolation instructions.
 */
function buildUserIdentityContext(actorId, channel) {
  const namespace = actorId.replace(/:/g, "_");

  return (
    "\n\n## Current User\n" +
    `You are chatting with user: ${actorId} (namespace: ${namespace}) on channel: ${channel}.\n` +
    `Always use "${namespace}" as the user_id when calling the s3-user-files skill.\n` +
    "\n## Per-User Isolation Rules (CRITICAL)\n" +
    "1. NEVER write to local files (MEMORY.md, IDENTITY.md, NOTES.md, etc.) " +
    "for storing persistent data. Local files are SHARED across all users.\n" +
    "2. For ALL persistent data (identity, preferences, notes, memories), " +
    "use the s3-user-files skill with the user_id shown above.\n" +
    "3. Your semantic memories about this user are automatically managed by " +
    "the memory system and already isolated per user.\n" +
    "4. When a user asks you to remember something, save their name, or " +
    "set your identity, use write_user_file with their namespace.\n" +
    "5. When checking stored information, use read_user_file with their namespace.\n" +
    "6. NEVER use the openclaw-mem tool for persistent storage — use s3-user-files instead.\n"
  );
}
```

**Step 2: Modify system prompt augmentation**

Find this block (around line 611-618):

```javascript
        // Build augmented system text if memory context is available
        let systemTextOverride = null;
        if (memoryContext) {
          const systemMessages = messages.filter((m) => m.role === "system");
          const baseSystemText = systemMessages.length > 0
            ? systemMessages.map((m) => m.content).join("\n")
            : SYSTEM_PROMPT;
          systemTextOverride = baseSystemText + memoryContext;
        }
```

Replace with:

```javascript
        // Build augmented system text with user identity + memory context
        // Identity is ALWAYS injected; memory context may be empty string
        const identityContext = buildUserIdentityContext(actorId, channel);
        const systemMessages = messages.filter((m) => m.role === "system");
        const baseSystemText = systemMessages.length > 0
          ? systemMessages.map((m) => m.content).join("\n")
          : SYSTEM_PROMPT;
        const systemTextOverride = baseSystemText + identityContext + memoryContext;
```

**Step 3: Verify syntax**

Run: `node -c bridge/agentcore-proxy.js`
Expected: No syntax errors.

**Step 4: Commit**

```bash
git add bridge/agentcore-proxy.js
git commit -m "feat: inject user identity and isolation rules into system prompt"
```

---

### Task 4: Update Dockerfile

**Files:**
- Modify: `bridge/Dockerfile`

**Step 1: Remove `openclaw-mem` from ClawHub installs**

Find line 22:
```dockerfile
    clawhub install openclaw-mem --no-input --force && \
```
Remove this line entirely (fix `&&` continuation on previous line).

**Step 2: Add `@aws-sdk/client-s3` to npm dependencies**

Modify the npm install block (lines 34-37). Add `@aws-sdk/client-s3`:

```dockerfile
RUN cd /app && npm init -y > /dev/null 2>&1 && \
    npm install @aws-sdk/client-bedrock-runtime \
                @aws-sdk/client-cognito-identity-provider \
                @aws-sdk/client-bedrock-agentcore \
                @aws-sdk/client-s3
```

**Step 3: Set NODE_PATH so skills can resolve packages**

Add after the npm install block:

```dockerfile
# Ensure skills can resolve npm packages from /app/node_modules
ENV NODE_PATH=/app/node_modules
```

**Step 4: Copy the custom S3 skill into the image**

Add after the `COPY entrypoint.sh` line:

```dockerfile
# Copy custom per-user file storage skill
COPY skills/s3-user-files /skills/s3-user-files
```

**Step 5: Commit**

```bash
git add bridge/Dockerfile
git commit -m "feat: add S3 user files skill to image, remove shared openclaw-mem"
```

---

### Task 5: Bump IMAGE_VERSION for Container Redeploy

**Files:**
- Modify: `stacks/agentcore_stack.py`

**Step 1: Bump IMAGE_VERSION**

In the `environment_variables` dict (around line 249), change:
```python
"IMAGE_VERSION": "10",
```
to:
```python
"IMAGE_VERSION": "11",
```

**Step 2: Commit**

```bash
git add stacks/agentcore_stack.py
git commit -m "chore: bump IMAGE_VERSION to 11 for per-user file isolation"
```

---

### Task 6: Update CLAUDE.md Documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Add per-user file storage to architecture diagram and docs**

Add S3 bucket to the architecture ASCII diagram. Add a new section:

```
### Per-User File Isolation
- S3 bucket (`openclaw-user-files-{account}-{region}`) stores files namespaced by `actorId`
- Custom `s3-user-files` skill provides read/write/list/delete operations
- Proxy injects `actorId` + isolation rules into every system prompt
- `openclaw-mem` was removed (replaced by AgentCore Memory + S3 skill)
- Built-in file tools still available but system prompt instructs Claude not to use them for persistence
```

Add to Gotchas:
```
### Per-User File Isolation
- **S3 skill requires NODE_PATH**: Set `NODE_PATH=/app/node_modules` in Dockerfile
- **openclaw-mem removed**: Shared SQLite replaced by per-user S3 + AgentCore Memory
- **System prompt injection**: `buildUserIdentityContext()` in proxy ALWAYS injects actorId; if missing, `default-user` triggers a reject in write.js
- **Content as CLI argument**: write.js receives content via `process.argv.slice(4).join(" ")` — works for typical .md files but may truncate very large content
```

Add to Key Configuration table:
```
| `user_files_ttl_days` | `365` | S3 file expiration |
```

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add per-user file isolation architecture to CLAUDE.md"
```

---

### Task 7: Build, Deploy, and Test

**Step 1: Run CDK synth**

```bash
cd /home/ec2-user/projects/openclaw-on-agentcore
source .venv/bin/activate
cdk synth
```

Expected: All 6 stacks synthesize without errors.

**Step 2: Build and push Docker image**

```bash
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export CDK_DEFAULT_REGION=us-west-2

docker build --platform linux/arm64 -t openclaw-bridge bridge/
aws ecr get-login-password --region $CDK_DEFAULT_REGION | \
  docker login --username AWS --password-stdin \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com
docker tag openclaw-bridge:latest \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
docker push \
  $CDK_DEFAULT_ACCOUNT.dkr.ecr.$CDK_DEFAULT_REGION.amazonaws.com/openclaw-bridge:latest
```

**Step 3: Deploy CDK stacks**

```bash
cdk deploy --all --require-approval never
```

**Step 4: Stop old runtime session (to pick up new image)**

```bash
RUNTIME_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAgentCore \
  --query "Stacks[0].Outputs[?OutputKey=='RuntimeId'].OutputValue" \
  --output text --region $CDK_DEFAULT_REGION)

# List sessions
aws bedrock-agentcore list-runtime-sessions \
  --agent-runtime-id $RUNTIME_ID \
  --region $CDK_DEFAULT_REGION

# Stop the active session (get session ID from list above)
aws bedrock-agentcore stop-runtime-session \
  --agent-runtime-id $RUNTIME_ID \
  --session-id <SESSION_ID> \
  --region $CDK_DEFAULT_REGION
```

The keepalive Lambda will start a new session with the updated image.

**Step 5: Verify S3 bucket was created**

```bash
aws s3 ls | grep openclaw-user-files
```

**Step 6: Test per-user isolation**

Manual test plan:

| Step | Action | Expected |
|------|--------|----------|
| 1 | Telegram User A: "Call me Alex, and your name is TeleBot" | Bot acknowledges, writes to S3 `telegram_AAAA/IDENTITY.md` |
| 2 | Telegram User A: "What's your name?" | Bot says "TeleBot" (reads from S3) |
| 3 | Slack User B: "What's your name?" | Bot does NOT say "TeleBot" — no file in `slack_BBBB/IDENTITY.md` |
| 4 | Slack User B: "Your name is SlackBot" | Bot acknowledges, writes to S3 `slack_BBBB/IDENTITY.md` |
| 5 | Telegram User A: "What's your name?" | Bot still says "TeleBot" (reads own namespace) |
| 6 | Check S3 | `aws s3 ls s3://openclaw-user-files-{account}-{region}/ --recursive` shows separate namespaces |

**Step 7: Verify CloudWatch logs**

Check for:
- `[proxy] Memory retrieval:` with distinct actorIds
- No `[proxy] WARNING: No user identity` messages
- No errors from S3 skill scripts

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Claude ignores isolation instructions and uses local files | Strong system prompt rules + removal of openclaw-mem reduces temptation. Monitor logs for local file writes. |
| S3 latency adds delay to file operations | S3 in same region adds ~20-50ms per call. Acceptable for file persistence operations. |
| SKILL.md format not parsed correctly by OpenClaw | Test skill loading in logs. OpenClaw verbose mode shows loaded skills. Adjust format if needed. |
| Content too large for CLI argument | For typical .md files (<10KB), CLI args work fine. For large content, consider stdin in a follow-up. |
| Existing MEMORY.md/IDENTITY.md in container | Container restarts clean ephemeral files. No migration needed — new data goes to S3. |
| `default-user` fallback bypasses isolation | write.js explicitly rejects `default_user`. System prompt warns about this. |

## Rollback Plan

If issues arise:
1. Revert the `openclaw-mem` removal in Dockerfile (re-add the `clawhub install` line)
2. Remove `identityContext` injection from proxy (revert to previous system prompt logic)
3. Leave S3 bucket in place (no harm) but it won't be used
4. Bump IMAGE_VERSION and redeploy
