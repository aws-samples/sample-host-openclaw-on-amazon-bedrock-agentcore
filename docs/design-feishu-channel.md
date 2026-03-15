# Design Document: Feishu (飞书) Channel Integration

**Author:** Research Branch
**Status:** Draft
**Branch:** `research/feishu-channel-integration`
**Date:** 2026-03-11

---

## 1. Background & Motivation

OpenClaw on AgentCore currently supports **Telegram** and **Slack** as messaging channels. Feishu (飞书, international version: Lark) is ByteDance's enterprise collaboration platform, widely used in China and Southeast Asia. Adding Feishu as a third channel would extend the bot's reach to a significant enterprise user base.

This document analyses the Feishu Bot API, maps it to the existing channel architecture, and proposes a concrete implementation plan.

> **Note on OpenClaw native Feishu support:** OpenClaw itself has native Feishu/Lark channel support
> (configurable via `channels.feishu` in `openclaw.json`, supporting both webhook and long-connection modes).
> However, our SaaS architecture runs OpenClaw in **headless mode** with no native channels enabled —
> all messages are bridged through the Router Lambda via WebSocket. The Feishu integration follows
> the same Router Lambda pattern as Telegram and Slack, not the native OpenClaw channel approach.

---

## 2. Existing Channel Architecture Summary

The current system follows a consistent pattern for each channel:

```
Channel App (Telegram/Slack)
    |
    v  (webhook HTTPS POST)
API Gateway HTTP API
    |  POST /webhook/{channel}
    v
Router Lambda
    |  1. Validate webhook signature
    |  2. Extract channel_user_id + message text
    |  3. Self-invoke async (return 200 immediately)
    |  4. Resolve user identity (DynamoDB)
    |  5. Invoke AgentCore Runtime
    |  6. Send response back via channel API
    v
AgentCore per-user microVM
```

### Per-Channel Touchpoints (what changes per channel):

| Component | What varies per channel |
|---|---|
| **Secrets Manager** | `openclaw/channels/{channel}` — credentials format differs |
| **API Gateway route** | `POST /webhook/{channel}` |
| **Router Lambda** | Webhook validation, message extraction, response sending |
| **CDK router_stack.py** | Route registration, env var for secret |
| **CDK security_stack.py** | Channel secret placeholder (already has `feishu` slot — but labeled differently) |
| **Cron Lambda** | `deliver_response()` channel dispatch |
| **Setup script** | `scripts/setup-{channel}.sh` |

### Current Secrets Manager Layout

The `security_stack.py` creates placeholder secrets for 4 channel names:
```python
channel_names = ["whatsapp", "telegram", "discord", "slack"]
```
However, **only Telegram and Slack are actually implemented** — WhatsApp and Discord are
placeholder secrets with no corresponding Lambda code, API Gateway routes, or setup scripts.
Feishu needs to be added to both the secret list and the implementation.

---

## 3. Feishu Bot API Analysis

### 3.1 Authentication Model

Feishu uses an **App ID + App Secret** model (unlike Telegram's single bot token or Slack's bot token + signing secret):

| Credential | Purpose | Storage Format |
|---|---|---|
| `app_id` | Application identifier | Part of JSON secret |
| `app_secret` | Application secret key | Part of JSON secret |
| `verification_token` | Webhook event verification (v1 legacy) | Part of JSON secret |
| `encrypt_key` | Webhook payload encryption/signature | Part of JSON secret |

**Access Token Flow:**
```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
Body: { "app_id": "...", "app_secret": "..." }
Response: { "tenant_access_token": "t-xxx", "expire": 7200 }
```

- Token expires in **2 hours** (7200 seconds)
- Must be cached and refreshed before expiry
- All API calls use `Authorization: Bearer {tenant_access_token}` header

**Comparison with existing channels:**

| Aspect | Telegram | Slack | Feishu |
|---|---|---|---|
| Auth credential | Single bot token (static) | Bot token + signing secret (static) | App ID + App Secret → tenant_access_token (dynamic, 2h TTL) |
| API auth header | Token embedded in URL | `Authorization: Bearer {bot_token}` | `Authorization: Bearer {tenant_access_token}` |
| Token refresh | None needed | None needed | Required every 2 hours |

### 3.2 Webhook Event Subscription

Feishu supports two modes for receiving events:
1. **HTTP Callback (webhook)** — same model as Telegram/Slack ✅ (recommended for this integration)
2. **WebSocket Long Connection** — persistent connection mode (not suitable for Lambda)

#### URL Verification Challenge

When registering the webhook URL in the Feishu developer console, Feishu sends a challenge request:

```json
POST /webhook/feishu
{
    "challenge": "ajls384kdjx98XX",
    "token": "xxxxxx",
    "type": "url_verification"
}
```

**Required response** (synchronous, must return immediately):
```json
{
    "challenge": "ajls384kdjx98XX"
}
```

This is analogous to Slack's `url_verification` challenge handling.

#### Webhook Signature Verification

Feishu v2 events include signature headers:

| Header | Description |
|---|---|
| `X-Lark-Request-Timestamp` | Unix timestamp |
| `X-Lark-Request-Nonce` | Random nonce |
| `X-Lark-Signature` | SHA-256 signature |

**Signature computation:**
```python
import hashlib

content = f"{timestamp}{nonce}{encrypt_key}".encode() + raw_body
expected_signature = hashlib.sha256(content).hexdigest()
```

Compare `expected_signature` with `X-Lark-Signature` header value.

#### Event Payload Encryption (Optional)

If `encrypt_key` is configured, the event body is AES-256-CBC encrypted:

```json
{
    "encrypt": "AES_CBC_encrypted_base64_string"
}
```

**Decryption:** AES-256-CBC with key derived from SHA-256(encrypt_key).

**Recommendation:** Enable encryption for production. The Router Lambda must handle both encrypted and plaintext payloads.

### 3.3 Required Event Subscriptions

The bot should subscribe to the following `im.message.*` events on the Feishu developer console:

| Event | Description | Priority |
|---|---|---|
| `im.message.receive_v1` | New message received (text, image, file, etc.) | **Phase 1 (MVP)** |
| `im.message.message_read_v1` | Message read receipt | Phase 3 (optional) |
| `im.message.reaction.created_v1` | Reaction added to message | Phase 3 (optional) |
| `im.message.reaction.deleted_v1` | Reaction removed | Phase 3 (optional) |
| `im.chat.member.bot.added_v1` | Bot added to a group chat | **Phase 1** (for group support) |
| `im.chat.member.bot.deleted_v1` | Bot removed from a group chat | **Phase 1** (for group support) |

For Phase 1 MVP, at minimum subscribe to: **`im.message.receive_v1`** and **`im.chat.member.bot.added_v1`**.

### 3.4 Required Bot Permissions

Configure the following permissions (scopes) in the Feishu developer console under "Permissions & Scopes":

| Permission | Scope ID | Purpose |
|---|---|---|
| Read messages | `im:message` | Receive message events |
| Send messages as bot | `im:message:send_as_bot` | Send replies |
| Read message content | `im:message.content:readonly` | Access message text/images |
| Read group info | `im:chat:readonly` | Identify group chat context |
| Read user info | `contact:user.base:readonly` | Get sender display name (optional) |
| Download images | `im:resource` | Download image attachments (Phase 2) |

### 3.5 Group Chat Support

Feishu bots can operate in both **P2P (单聊)** and **Group (群聊)** modes. The `chat_type` field in the event distinguishes them:

| `chat_type` | Meaning | Behavior |
|---|---|---|
| `p2p` | Direct message to bot | All messages are directed to the bot |
| `group` | Group chat | Bot only receives messages when **@mentioned** |

**Group chat considerations:**
- In group chats, the `text` content includes `@_user_1` mention tags — these need to be stripped before passing to AgentCore
- The `chat_id` (`oc_xxxx`) is the group ID, not the sender's P2P chat — responses go back to the group
- The `sender.sender_id.open_id` still identifies the individual user for identity resolution
- For identity mapping, we use `feishu:{open_id}` (sender), not `feishu:{chat_id}` (group)
- `channel_target` (where to send replies) should be the `chat_id` — works for both P2P and group

**Phase 1 recommendation:** Support both P2P and group chat from the start. The message extraction logic just needs to:
1. Check for `@bot` mentions in group messages (strip the mention tag)
2. Use `chat_id` as `channel_target` for replies (same API for both P2P and group)

### 3.6 Feishu App Lifecycle

**Important:** A Feishu app must be **published (发布)** before it becomes discoverable and usable by other users. During development, only the app creator can interact with the bot.

Steps on the Feishu developer console:
1. **Create Application** (创建应用) — self-built app (企业自建应用)
2. **Add Bot capability** (添加机器人能力)
3. **Configure Permissions** (权限管理) — see Section 3.4
4. **Configure Events** (事件与回调 → 事件配置):
   - Set "Send events to" (将事件发送至): **Developer Server** (开发者服务器)
   - Set Request URL to: `https://{api-gateway-url}/webhook/feishu`
   - Add events listed in Section 3.3
5. **Publish the App** (发布应用) — submit for review/approval
   - After publishing, the bot is searchable on Feishu by all users in the tenant
   - Until published, only the developer can test it

### 3.7 Event Payload Structure (v2 Schema)

Message receive event (`im.message.receive_v1`):

```json
{
    "schema": "2.0",
    "header": {
        "event_id": "unique_event_id",
        "token": "verification_token",
        "create_time": "1234567890",
        "event_type": "im.message.receive_v1",
        "tenant_key": "tenant_key",
        "app_id": "cli_xxxx"
    },
    "event": {
        "sender": {
            "sender_id": {
                "open_id": "ou_xxxx",
                "user_id": "on_xxxx",
                "union_id": "on_xxxx"
            },
            "sender_type": "user",
            "tenant_key": "tenant_key"
        },
        "message": {
            "message_id": "om_xxxx",
            "root_id": "",
            "parent_id": "",
            "create_time": "1234567890",
            "chat_id": "oc_xxxx",
            "chat_type": "p2p",
            "message_type": "text",
            "content": "{\"text\":\"Hello\"}"
        }
    }
}
```

**Key fields mapping:**

| Our concept | Feishu field | Notes |
|---|---|---|
| Channel user ID | `event.sender.sender_id.open_id` | Stable per-app user identifier |
| Chat ID (for replies) | `event.message.chat_id` | Used as `receive_id` when sending (works for both P2P and group) |
| Chat type | `event.message.chat_type` | `p2p` (direct) or `group` (group chat) |
| Message text | `event.message.content` | JSON string, parse to extract `text` |
| Message type | `event.message.message_type` | `text`, `image`, `file`, etc. |
| Event dedup ID | `header.event_id` | For idempotency |
| @mentions (group) | `event.message.mentions` | Array of `{key, id, name}` — strip from text |

### 3.8 Sending Messages API

```
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
Authorization: Bearer {tenant_access_token}
Content-Type: application/json

{
    "receive_id": "oc_xxxx",
    "msg_type": "text",
    "content": "{\"text\":\"Hello from bot\"}"
}
```

**Response:**
```json
{
    "code": 0,
    "msg": "success",
    "data": {
        "message_id": "om_xxxx",
        ...
    }
}
```

**Message size limits:**
- Text messages: content JSON max ~30KB
- For long responses, must split into multiple messages (similar to Telegram's 4096 char limit)

**Feishu-specific rich content:** Feishu uses its own rich text format ("post" type) with structured JSON, and also supports Markdown in certain contexts. For initial implementation, plain text is sufficient; Markdown-to-Feishu conversion can be a follow-up.

### 3.9 Image Upload Support (Phase 1)

Feishu image handling follows the same pattern as Telegram/Slack (download → S3 → Bedrock multimodal):

1. **Receiving images:** The event has `message_type: "image"` with content `{"image_key": "img_v3_xxxx"}`
2. **Download image:** `GET /open-apis/im/v1/images/{image_key}` with `Authorization: Bearer {tenant_access_token}`
3. **Upload to S3:** Same as Telegram/Slack — `{namespace}/_uploads/img_{ts}_{hex}.{ext}`
4. **Pass to AgentCore:** Structured message with `images[{s3Key, contentType}]` (existing flow)

**Download API details:**
```
GET https://open.feishu.cn/open-apis/im/v1/images/{image_key}
Authorization: Bearer {tenant_access_token}
```
Response: raw image bytes with `Content-Type` header (e.g. `image/jpeg`)

**Implementation in Router Lambda:**
```python
def _download_feishu_image(message_content, msg_type):
    """Download image from Feishu API using image_key.

    Returns (image_bytes, content_type, filename) or (None, None, None).
    """
    if msg_type != "image":
        return None, None, None

    try:
        content = json.loads(message_content) if isinstance(message_content, str) else message_content
        image_key = content.get("image_key", "")
    except (json.JSONDecodeError, TypeError):
        return None, None, None

    if not image_key:
        return None, None, None

    token = _get_feishu_tenant_token()
    if not token:
        return None, None, None

    domain = os.environ.get("FEISHU_API_DOMAIN", "https://open.feishu.cn")
    url = f"{domain}/open-apis/im/v1/images/{image_key}"
    req = urllib_request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = urllib_request.urlopen(req, timeout=30)
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        image_bytes = resp.read(4 * 1024 * 1024)  # 4MB max
        ext = content_type.split("/")[-1].split(";")[0]
        filename = f"feishu_{image_key}.{ext}"
        return image_bytes, content_type, filename
    except Exception as e:
        logger.error("Failed to download Feishu image %s: %s", image_key, e)
        return None, None, None
```

Reuses the existing `_upload_image_to_s3()` function — no changes needed there.

**Supported types:** `image/jpeg`, `image/png`, `image/gif`, `image/webp` (same as Telegram/Slack, max 3.75 MB per Bedrock limit).

**Required permission:** `im:resource` scope (see Section 3.4).

### 3.10 Rate Limits

| API | Rate Limit |
|---|---|
| Send message | 5 messages/second per app (1000/min for tenant) |
| Get tenant_access_token | 500/min |
| Download image | Varies by endpoint |

These are well within our usage patterns (per-user bot, not bulk messaging).

---

## 4. Proposed Implementation

### 4.1 Secrets Manager Secret Format

Store in `openclaw/channels/feishu` as JSON:

```json
{
    "appId": "cli_xxxx",
    "appSecret": "xxxx",
    "verificationToken": "xxxx",
    "encryptKey": "xxxx"
}
```

### 4.2 Router Lambda Changes

#### 4.2.1 New Environment Variable

```python
FEISHU_TOKEN_SECRET_ID = os.environ.get("FEISHU_TOKEN_SECRET_ID", "")
```

#### 4.2.2 Feishu Credentials Helper

```python
def _get_feishu_credentials():
    """Return (app_id, app_secret, verification_token, encrypt_key) from Feishu secret."""
    raw = _get_secret(FEISHU_TOKEN_SECRET_ID)
    if not raw:
        return "", "", "", ""
    try:
        data = json.loads(raw)
        return (
            data.get("appId", ""),
            data.get("appSecret", ""),
            data.get("verificationToken", ""),
            data.get("encryptKey", ""),
        )
    except (json.JSONDecodeError, TypeError):
        return "", "", "", ""
```

#### 4.2.3 Tenant Access Token Cache

Unlike Telegram/Slack where tokens are static, Feishu requires a dynamic `tenant_access_token` with 2-hour TTL:

```python
_feishu_token_cache = {"token": "", "expires_at": 0}

def _get_feishu_tenant_token():
    """Get or refresh Feishu tenant_access_token (2h TTL, refresh 5 min early)."""
    if _feishu_token_cache["token"] and time.time() < _feishu_token_cache["expires_at"] - 300:
        return _feishu_token_cache["token"]

    app_id, app_secret, _, _ = _get_feishu_credentials()
    if not app_id or not app_secret:
        logger.error("Feishu app_id/app_secret not configured")
        return ""

    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        resp = urllib_request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("code") == 0:
            token = result["tenant_access_token"]
            expire = result.get("expire", 7200)
            _feishu_token_cache["token"] = token
            _feishu_token_cache["expires_at"] = time.time() + expire
            return token
        logger.error("Feishu token error: %s", result.get("msg", ""))
    except Exception as e:
        logger.error("Failed to get Feishu tenant_access_token: %s", e)
    return ""
```

#### 4.2.4 Webhook Validation

```python
def validate_feishu_webhook(headers, body_bytes):
    """Validate Feishu webhook using X-Lark-Signature SHA-256 verification.

    Signature = SHA256(timestamp + nonce + encrypt_key + body)
    Returns False (fail-closed) if encrypt_key is not configured.
    """
    _, _, _, encrypt_key = _get_feishu_credentials()
    if not encrypt_key:
        logger.error("Feishu encrypt_key not configured — rejecting request (fail-closed)")
        return False

    timestamp = headers.get("x-lark-request-timestamp", "")
    nonce = headers.get("x-lark-request-nonce", "")
    signature = headers.get("x-lark-signature", "")

    if not timestamp or not nonce or not signature:
        logger.warning("Feishu webhook missing signature headers")
        return False

    content = f"{timestamp}{nonce}{encrypt_key}".encode() + body_bytes
    expected = hashlib.sha256(content).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.warning("Feishu webhook signature mismatch")
        return False

    return True
```

#### 4.2.5 Event Payload Decryption

```python
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

def _decrypt_feishu_event(encrypted_str, encrypt_key):
    """Decrypt AES-256-CBC encrypted Feishu event payload."""
    key = hashlib.sha256(encrypt_key.encode()).digest()
    encrypted = base64.b64decode(encrypted_str)
    iv = encrypted[:16]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted[16:]) + decryptor.finalize()
    # Remove PKCS7 padding
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode("utf-8")
```

**Note:** This requires `cryptography` library. Since the Router Lambda is Python, we need to add it to the Lambda layer or bundle. Alternatively, implement AES decryption with the standard library's `Crypto` module, or consider making encryption optional during initial development.

**Simpler alternative for initial implementation:** Configure the Feishu app with an empty `encrypt_key` (encryption disabled). This avoids the AES dependency. Signature verification via `X-Lark-Signature` still works with the `encrypt_key` for signing without enabling payload encryption. **However,** the Feishu documentation recommends enabling encryption for production — so this should be treated as a Phase 2 enhancement.

#### 4.2.6 Message Extraction

```python
def _handle_feishu_webhook(body, headers, body_bytes):
    """Handle Feishu webhook event."""

    # Handle encrypted payloads
    if "encrypt" in body:
        _, _, _, encrypt_key = _get_feishu_credentials()
        decrypted = _decrypt_feishu_event(body["encrypt"], encrypt_key)
        body = json.loads(decrypted)

    # URL verification challenge (like Slack's url_verification)
    if body.get("type") == "url_verification":
        return {
            "statusCode": 200,
            "body": json.dumps({"challenge": body.get("challenge", "")}),
            "headers": {"Content-Type": "application/json"},
        }

    # Validate signature (skip for url_verification)
    if not validate_feishu_webhook(headers, body_bytes):
        return {"statusCode": 401, "body": "Unauthorized"}

    # Extract v2 event
    schema = body.get("schema", "")
    header = body.get("header", {})
    event = body.get("event", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        logger.info("Ignoring Feishu event type: %s", event_type)
        return {"statusCode": 200, "body": "OK"}

    # Ignore bot messages (same as Slack's bot_id check)
    sender = event.get("sender", {})
    if sender.get("sender_type") != "user":
        return {"statusCode": 200, "body": "OK"}

    # Extract sender and message
    sender_id = sender.get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    msg_type = message.get("message_type", "")
    content_str = message.get("content", "{}")

    # Parse message content
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {}

    if msg_type == "text":
        text = content.get("text", "")
    elif msg_type == "image":
        text = ""  # Image-only message — text may be empty
        # Image download + S3 upload handled below (same flow as Telegram/Slack)
    else:
        text = content.get("text", str(content))

    # Group chat: strip @bot mention tags (e.g. "@_user_1 hello" → "hello")
    chat_type = message.get("chat_type", "p2p")
    if chat_type == "group":
        mentions = message.get("mentions", [])
        for mention in mentions:
            mention_key = mention.get("key", "")
            if mention_key:
                text = text.replace(mention_key, "").strip()
        # In group chat, ignore messages that don't @mention the bot
        # (after stripping mentions, if text is empty, the user only @'d the bot)
        if not text:
            text = "hi"  # Default prompt when only @mentioned with no text

    if not sender_id or not text:
        return {"statusCode": 200, "body": "OK"}

    # Event dedup using header.event_id — optional but recommended
    # (similar pattern to Slack's x-slack-retry-num)

    return {
        "channel": "feishu",
        "channel_user_id": sender_id,
        "chat_id": chat_id,  # Works for both P2P and group — replies go to same chat
        "text": text,
        "event_id": header.get("event_id", ""),
    }
```

#### 4.2.7 Progress Notification (matching Slack pattern)

```python
def _feishu_progress_notify(chat_id, stop_event, notify_after_s=30):
    """Send a one-time progress message after waiting notify_after_s seconds.

    Same pattern as _slack_progress_notify — lets the user know the bot
    is still working during long AgentCore invocations (subagent tasks, etc.).
    """
    if stop_event.wait(notify_after_s):
        return  # AgentCore responded before timeout
    send_feishu_message(chat_id, "Working on your request...")
```

Used in `handle_feishu()` the same way Slack uses it:
```python
stop_notify = threading.Event()
notify_thread = threading.Thread(
    target=_feishu_progress_notify,
    args=(chat_id, stop_notify),
    daemon=True,
)
notify_thread.start()
try:
    result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, "feishu", agent_message)
finally:
    stop_notify.set()
    notify_thread.join(timeout=2)
```

#### 4.2.8 Send Response

```python
def send_feishu_message(chat_id, text):
    """Send a message via Feishu Bot API."""
    token = _get_feishu_tenant_token()
    if not token:
        logger.error("No Feishu tenant_access_token available")
        return

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"

    # For long messages, Feishu text content has ~30KB limit
    # Split if needed (similar to Telegram's 4096 char split)
    MAX_FEISHU_TEXT_LEN = 20000  # Conservative limit

    chunks = [text[i:i+MAX_FEISHU_TEXT_LEN] for i in range(0, len(text), MAX_FEISHU_TEXT_LEN)] if len(text) > MAX_FEISHU_TEXT_LEN else [text]

    for chunk in chunks:
        data = json.dumps({
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }).encode()

        req = urllib_request.Request(url, data=data, headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        })
        try:
            urllib_request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("Failed to send Feishu message to %s: %s", chat_id, e)
```

### 4.3 CDK Infrastructure Changes

#### 4.3.1 security_stack.py

Add `"feishu"` to the channel list:

```python
channel_names = ["whatsapp", "telegram", "discord", "slack", "feishu"]
```

This creates `openclaw/channels/feishu` in Secrets Manager.

#### 4.3.2 router_stack.py

1. **Add API Gateway route:**
```python
self.http_api.add_routes(
    path="/webhook/feishu",
    methods=[apigwv2.HttpMethod.POST],
    integration=lambda_integration,
)
```

2. **Add Lambda environment variable:**
```python
"FEISHU_TOKEN_SECRET_ID": feishu_token_secret_name,
```

3. **Update constructor parameters** to accept `feishu_token_secret_name`.

4. **Update cdk-nag suppression** reason strings to include Feishu.

#### 4.3.3 app.py

Pass `feishu_token_secret_name` from SecurityStack to RouterStack.

#### 4.3.4 cron_stack.py

Add `FEISHU_TOKEN_SECRET_ID` env var to cron executor Lambda.

### 4.4 Cron Lambda Changes

Add Feishu message delivery to `deliver_response()`:

```python
def deliver_response(channel, channel_target, response_text):
    response_text = _extract_text_from_content_blocks(response_text)

    if channel == "telegram":
        # ... existing ...
    elif channel == "slack":
        # ... existing ...
    elif channel == "feishu":
        send_feishu_message(channel_target, response_text)
    else:
        logger.warning("Unknown channel type: %s", channel)
```

The cron Lambda also needs the `_get_feishu_tenant_token()` helper and `send_feishu_message()` function. Consider extracting shared channel utilities into a Lambda layer to avoid duplication.

### 4.5 Setup Script

Create `scripts/setup-feishu.sh`:

```bash
#!/bin/bash
# Set up Feishu Bot event subscription and add deployer to allowlist.
#
# Prerequisites:
#   - Feishu app created at https://open.feishu.cn/app
#   - App credentials stored in Secrets Manager (openclaw/channels/feishu)

set -euo pipefail
REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
TABLE_NAME="${IDENTITY_TABLE_NAME:-openclaw-identity}"

echo "=== OpenClaw Feishu Setup ==="

# Step 1: Display webhook URL
API_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawRouter \
    --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
    --output text --region "$REGION")
WEBHOOK_URL="${API_URL}webhook/feishu"

echo "Your Feishu webhook URL is:"
echo "  $WEBHOOK_URL"
echo ""
echo "Configure in Feishu Developer Console (https://open.feishu.cn/app):"
echo ""
echo "  Step A: Create & configure the app (if not done yet)"
echo "    1. Create a self-built app (企业自建应用)"
echo "    2. Add Bot capability (添加机器人能力)"
echo "    3. Permissions (权限管理) → add these scopes:"
echo "       - im:message              (receive messages)"
echo "       - im:message:send_as_bot  (send messages as bot)"
echo "       - im:message.content:readonly (read message content)"
echo "       - im:chat:readonly        (read group info)"
echo "       - im:resource             (download images — Phase 2)"
echo ""
echo "  Step B: Configure event subscriptions"
echo "    1. Event Subscriptions (事件与回调 → 事件配置)"
echo "    2. Set 'Send events to' → Developer Server (开发者服务器)"
echo "    3. Set Request URL to:"
echo "       $WEBHOOK_URL"
echo "    4. Add events:"
echo "       - im.message.receive_v1          (required)"
echo "       - im.chat.member.bot.added_v1    (recommended)"
echo "       - im.chat.member.bot.deleted_v1  (recommended)"
echo ""
echo "  Step C: Publish the app (发布应用)"
echo "    - The bot is NOT usable by others until published!"
echo "    - After publishing, users can search and add the bot on Feishu"
echo ""
read -rp "Press Enter once you've completed the above steps..."
echo ""

# Step 2: Store credentials
read -rp "Enter your Feishu App ID: " APP_ID
read -rp "Enter your Feishu App Secret: " APP_SECRET
read -rp "Enter your Verification Token: " VERIFICATION_TOKEN
read -rp "Enter your Encrypt Key: " ENCRYPT_KEY

aws secretsmanager update-secret \
    --secret-id openclaw/channels/feishu \
    --secret-string "{\"appId\":\"${APP_ID}\",\"appSecret\":\"${APP_SECRET}\",\"verificationToken\":\"${VERIFICATION_TOKEN}\",\"encryptKey\":\"${ENCRYPT_KEY}\"}" \
    --region "$REGION"

# Step 3: Add to allowlist
echo ""
echo "To find your Feishu open_id, message the bot — the rejection reply will show your ID."
read -rp "Enter your Feishu open_id (e.g. ou_xxxx): " FEISHU_USER_ID
CHANNEL_KEY="feishu:${FEISHU_USER_ID}"
NOW_ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

aws dynamodb put-item \
    --table-name "$TABLE_NAME" \
    --region "$REGION" \
    --item "{
        \"PK\": {\"S\": \"ALLOW#${CHANNEL_KEY}\"},
        \"SK\": {\"S\": \"ALLOW\"},
        \"channelKey\": {\"S\": \"${CHANNEL_KEY}\"},
        \"addedAt\": {\"S\": \"${NOW_ISO}\"}
    }"

echo ""
echo "=== Setup complete ==="
echo "  Webhook URL: $WEBHOOK_URL"
echo "  Allowlisted: $CHANNEL_KEY"
```

### 4.6 Feishu vs. Lark Domain Handling

Feishu (China) and Lark (International) use different API domains:

| Version | Domain |
|---|---|
| Feishu (飞书) | `https://open.feishu.cn` |
| Lark (国际版) | `https://open.larksuite.com` |

**Recommendation:** Add `feishu_api_domain` to `cdk.json` context (default: `https://open.feishu.cn`). Pass as Lambda env var. All API URLs in the Lambda use this base domain. Users deploying for Lark override it to `https://open.larksuite.com`.

---

## 5. DynamoDB Identity Mapping

Feishu users follow the same pattern as Telegram/Slack:

| PK | SK | Example |
|---|---|---|
| `CHANNEL#feishu:ou_xxxx` | `PROFILE` | Channel→user lookup |
| `USER#user_abc123` | `CHANNEL#feishu:ou_xxxx` | User's bound Feishu channel |
| `ALLOW#feishu:ou_xxxx` | `ALLOW` | Feishu user allowlist entry |

Cross-channel binding works identically — a Feishu user can generate a bind code and link to their Telegram/Slack identity.

---

## 6. Architecture Diagram (Updated)

```
                                                    +--------------------------------+
                                                    |          End Users             |
                                                    |  Telegram   Slack    Feishu    |
                                                    +--+----------+--------+--------+
                                                       |          |        |
                                            (webhook HTTPS over internet)
                                                       |          |        |
+------------------------------------------------------+----------+--------+---------+
|  AWS Account                                                                       |
|                                                                                    |
|  +----------------------------------------------+                                 |
|  |  API Gateway HTTP API                        |                                 |
|  |  (openclaw-router)                           |                                 |
|  |                                              |                                 |
|  |  POST /webhook/telegram  --> Lambda          |                                 |
|  |  POST /webhook/slack     --> Lambda          |                                 |
|  |  POST /webhook/feishu    --> Lambda          |                                 |
|  |  GET  /health            --> Lambda          |                                 |
|  +----------------------+-----------------------+                                 |
|                         |                                                          |
|  +----------------------v-----------------------+                                 |
|  |  Router Lambda (openclaw-router)             |                                 |
|  |                                              |                                 |
|  |  1. Validate webhook:                        |                                 |
|  |     - Telegram: X-Telegram-Bot-Api-Secret-   |                                 |
|  |       Token header                           |                                 |
|  |     - Slack: X-Slack-Signature HMAC-SHA256   |                                 |
|  |     - Feishu: X-Lark-Signature SHA-256       |                                 |
|  |  2. Self-invoke async                        |                                 |
|  |  3. Resolve user in DynamoDB                 |                                 |
|  |  4. Get/create AgentCore session             |                                 |
|  |  5. InvokeAgentRuntime                       |                                 |
|  |  6. Send response back to channel API        |                                 |
|  |     - Feishu: requires tenant_access_token   |                                 |
|  |       (2h TTL, auto-refreshed)               |                                 |
|  +----------------------------------------------+                                 |
```

---

## 7. Key Differences from Telegram/Slack

| Aspect | Impact | Mitigation |
|---|---|---|
| **Dynamic token** (tenant_access_token, 2h TTL) | Must cache and refresh in Lambda | In-memory cache with early refresh (5 min before expiry). Works well with Lambda warm instances. Cold starts fetch a new token (~100ms) |
| **URL verification challenge** | Must handle synchronously (like Slack) | Return `{"challenge": "..."}` for `type: url_verification` before async self-invoke |
| **Payload encryption** (AES-256-CBC) | Requires `cryptography` library | Phase 1: disable encryption. Phase 2: add as Lambda layer |
| **Message content is JSON string** | `content` field is `"{\"text\":\"...\"}}"` | Parse JSON string to extract text |
| **Sender ID is `open_id`** | Different from Telegram's numeric ID or Slack's `U...` ID | Map as `feishu:{open_id}` in DynamoDB |
| **Feishu vs Lark domains** | API base URL differs | Configurable via `cdk.json` context |
| **Group chat support** | Bot can be @mentioned in groups (`chat_type: "group"`) | Strip @mention tags from text; use `chat_id` for replies; identity still from `sender.open_id` |
| **App publishing required** | Bot not usable until published on Feishu console | Document in setup script; dev testing works pre-publish for app creator only |
| **Event deduplication** | Feishu may retry events | Use `header.event_id` for idempotency check (optional, DynamoDB conditional write with TTL) |

---

## 8. Implementation Phases

### Phase 1: Basic Text Messaging (MVP)

**Scope:**
- CDK: Add `feishu` secret, API Gateway route, Lambda env vars
- Router Lambda: Feishu webhook validation, URL verification challenge, text + image message handling, response sending
- Image support: Download via Feishu API → S3 upload → Bedrock multimodal (same as Telegram/Slack)
- Group chat: P2P and @mention in group both supported
- Cron Lambda: Feishu message delivery
- Setup script: `scripts/setup-feishu.sh`
- Tests: Unit tests for webhook validation, message extraction, image download, response sending

**Not in scope:** Payload encryption, rich text formatting, Lark domain support.

**Includes group chat:** P2P and group chat both supported from the start — group messages with @bot mention are routed to AgentCore, replies go back to the group via `chat_id`.

**Estimated changes:**
| File | Change Type | Size |
|---|---|---|
| `stacks/security_stack.py` | Add `"feishu"` to channel list | 1 line |
| `stacks/router_stack.py` | Add route + env var + constructor param | ~20 lines |
| `app.py` | Pass feishu secret name | ~3 lines |
| `stacks/cron_stack.py` | Add env var | ~2 lines |
| `lambda/router/index.py` | Feishu handlers (validation, extract, send, token cache, image download) | ~200 lines |
| `lambda/cron/index.py` | Feishu delivery | ~40 lines |
| `scripts/setup-feishu.sh` | New file | ~60 lines |
| `lambda/router/test_feishu.py` | Unit tests | ~150 lines |
| `docs/architecture.md` | Update diagrams | ~20 lines |
| `CLAUDE.md` | Update docs | ~30 lines |

### Phase 2: Encryption + Event Deduplication

- AES-256-CBC payload decryption (add `cryptography` to Lambda layer)
- Event deduplication via DynamoDB conditional write + TTL

### Phase 3: Rich Text + Lark Support

- Markdown-to-Feishu "post" (rich text) conversion
- Configurable `feishu_api_domain` for Lark international
- Typing indicator (Feishu doesn't have a native typing API — may use message update)

---

## 9. Testing Plan

### Unit Tests (`lambda/router/test_feishu.py`)

```python
# Test cases:
# 1. validate_feishu_webhook — valid signature → True
# 2. validate_feishu_webhook — invalid signature → False
# 3. validate_feishu_webhook — missing headers → False
# 4. _handle_feishu_webhook — url_verification challenge → returns challenge
# 5. _handle_feishu_webhook — text message → extracts sender_id, chat_id, text
# 6. _handle_feishu_webhook — non-message event → returns 200 OK (ignored)
# 7. _get_feishu_tenant_token — token cached → returns cached
# 8. _get_feishu_tenant_token — token expired → fetches new
# 9. send_feishu_message — long text → splits into chunks
# 10. send_feishu_message — API error → logs and continues
```

### E2E Tests

Extend `tests/e2e/bot_test.py` with Feishu webhook simulation (similar to existing Telegram tests).

---

## 10. Security Considerations

| Concern | Mitigation |
|---|---|
| Feishu credentials exposure | Stored in Secrets Manager with KMS CMK encryption |
| Webhook replay attacks | Signature verification includes timestamp+nonce |
| Token leakage | `tenant_access_token` cached in Lambda memory only, 2h TTL, never logged |
| Cross-tenant access | `open_id` is app-scoped (different per Feishu app), preventing cross-app user impersonation |
| Payload interception | Enable AES encryption in Phase 2 for defense-in-depth |

---

## 11. Open Questions

1. **Lambda dependency:** The `cryptography` library for AES decryption is not in the standard Lambda Python runtime. Options:
   - a) Lambda layer with `cryptography` pre-built for AL2023 arm64
   - b) Pure-Python AES implementation (slower, more code)
   - c) Disable encryption in Phase 1 (recommended)

2. **Event deduplication:** Feishu may retry failed webhook deliveries. Should we implement dedup via DynamoDB conditional write + TTL, or rely on the existing idempotent user resolution?

3. **Feishu app permissions:** The bot needs `im:message` (receive) and `im:message:send_as_bot` (send) permissions. Should we document the full permission list in the setup script?

4. **Rich text format:** Feishu's native rich text ("post" type) is different from Markdown. The current `_markdown_to_telegram_html()` approach won't work directly. Should Phase 3 implement a `_markdown_to_feishu_post()` converter?

5. **Code duplication:** The cron Lambda duplicates channel-specific send functions from the router Lambda. Adding a third channel increases this duplication. Should we extract shared code into a Lambda layer?
