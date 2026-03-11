"""Router Lambda — Webhook ingestion for Telegram and Slack.

Receives webhook events via API Gateway HTTP API, resolves user identity via
DynamoDB, invokes the per-user AgentCore Runtime session, and sends responses
back to the originating channel.

Path routing:
  POST /webhook/telegram  — Telegram Bot API webhook
  POST /webhook/slack     — Slack Events API webhook
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from urllib import request as urllib_request
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
AGENTCORE_QUALIFIER = os.environ["AGENTCORE_QUALIFIER"]
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
TELEGRAM_TOKEN_SECRET_ID = os.environ.get("TELEGRAM_TOKEN_SECRET_ID", "")
SLACK_TOKEN_SECRET_ID = os.environ.get("SLACK_TOKEN_SECRET_ID", "")
WEBHOOK_SECRET_ID = os.environ.get("WEBHOOK_SECRET_ID", "")
LAMBDA_FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
AWS_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"
LAMBDA_TIMEOUT_SECONDS = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "600"))

# --- Clients (lazy init on cold start) ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(
        read_timeout=max(LAMBDA_TIMEOUT_SECONDS - 15, 60),
        connect_timeout=10,
        retries={"max_attempts": 0},
    ),
)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)

USER_FILES_BUCKET = os.environ.get("USER_FILES_BUCKET", "")

# --- Token cache (survives across warm invocations, 15-min TTL) ---
_SECRET_CACHE_TTL_SECONDS = 900  # 15 minutes
_token_cache = {}  # {secret_id: (value, fetched_at)}

BIND_CODE_TTL_SECONDS = 600  # 10 minutes

# --- Screenshot marker detection ---
SCREENSHOT_MARKER_RE = re.compile(r"\[SCREENSHOT:([^\]]+)\]")


def _get_secret(secret_id):
    """Fetch a secret value, cached with a 15-minute TTL."""
    cached = _token_cache.get(secret_id)
    if cached:
        value, fetched_at = cached
        if time.time() - fetched_at < _SECRET_CACHE_TTL_SECONDS:
            return value
    if not secret_id:
        return ""
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        value = resp["SecretString"]
        _token_cache[secret_id] = (value, time.time())
        return value
    except Exception as e:
        logger.warning("Failed to fetch secret %s: %s", secret_id, e)
        return ""


def _get_telegram_token():
    return _get_secret(TELEGRAM_TOKEN_SECRET_ID)


def _get_slack_tokens():
    """Return (bot_token, signing_secret) tuple from Slack secret (JSON or plain string)."""
    raw = _get_secret(SLACK_TOKEN_SECRET_ID)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        return data.get("botToken", ""), data.get("signingSecret", "")
    except (json.JSONDecodeError, TypeError):
        return raw, ""


def _get_webhook_secret():
    return _get_secret(WEBHOOK_SECRET_ID)


# ---------------------------------------------------------------------------
# Webhook validation helpers
# ---------------------------------------------------------------------------

def validate_telegram_webhook(headers):
    """Validate Telegram webhook using X-Telegram-Bot-Api-Secret-Token header.

    Returns False (fail-closed) if no webhook secret is configured.
    """
    webhook_secret = _get_webhook_secret()
    if not webhook_secret:
        logger.error("WEBHOOK_SECRET_ID not configured — rejecting request (fail-closed)")
        return False

    token = headers.get("x-telegram-bot-api-secret-token", "")
    if not token:
        logger.warning("Telegram webhook missing X-Telegram-Bot-Api-Secret-Token header")
        return False

    if not hmac.compare_digest(token, webhook_secret):
        logger.warning("Telegram webhook secret token mismatch")
        return False

    return True


def validate_slack_webhook(headers, body):
    """Validate Slack webhook using X-Slack-Signature HMAC-SHA256 verification.

    Slack signs each request with: v0=HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
    See: https://api.slack.com/authentication/verifying-requests-from-slack

    Returns False (fail-closed) if no signing secret is configured.
    """
    _, signing_secret = _get_slack_tokens()
    if not signing_secret:
        logger.error("Slack signing secret not configured — rejecting request (fail-closed)")
        return False

    timestamp = headers.get("x-slack-request-timestamp", "")
    signature = headers.get("x-slack-signature", "")

    if not timestamp or not signature:
        logger.warning("Slack webhook missing timestamp or signature headers")
        return False

    # Reject requests older than 5 minutes to prevent replay attacks
    try:
        if abs(time.time() - int(timestamp)) > 300:
            logger.warning("Slack webhook timestamp too old (replay attack prevention)")
            return False
    except (ValueError, TypeError):
        logger.warning("Slack webhook invalid timestamp: %s", timestamp)
        return False

    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.warning("Slack webhook signature mismatch")
        return False

    return True


# ---------------------------------------------------------------------------
# DynamoDB identity helpers
# ---------------------------------------------------------------------------

def is_user_allowed(channel, channel_user_id):
    """Check if a new user is permitted to register.

    Returns True if:
    - REGISTRATION_OPEN is true (anyone can register), OR
    - An ALLOW#{channel}:{channel_user_id} record exists in DynamoDB

    This is only called for NEW users (no existing CHANNEL# record).
    Existing users and bind-code redemptions bypass this check.
    """
    if REGISTRATION_OPEN:
        return True
    channel_key = f"{channel}:{channel_user_id}"
    try:
        resp = identity_table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
        if "Item" in resp:
            return True
    except ClientError as e:
        logger.error("Allowlist check failed: %s", e)
    return False


def resolve_user(channel, channel_user_id, display_name=""):
    """Look up or create a user for the given channel identity.

    Returns (user_id, is_new). Returns (None, False) if user is not allowed.
    """
    channel_key = f"{channel}:{channel_user_id}"
    pk = f"CHANNEL#{channel_key}"

    # 1. Try to find existing mapping
    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        if "Item" in resp:
            return resp["Item"]["userId"], False
    except ClientError as e:
        logger.error("DynamoDB get_item failed: %s", e)

    # 2. Check allowlist before creating a new user
    if not is_user_allowed(channel, channel_user_id):
        logger.warning("User %s not on allowlist — rejecting registration", channel_key)
        return None, False

    # 3. Create new user (conditional write to handle race conditions)
    user_id = f"user_{uuid.uuid4().hex[:16]}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        # User profile
        identity_table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "PROFILE",
                "userId": user_id,
                "createdAt": now_iso,
                "displayName": display_name or channel_user_id,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.error("Failed to create user profile: %s", e)

    try:
        # Channel -> user mapping (conditional to prevent race)
        identity_table.put_item(
            Item={
                "PK": pk,
                "SK": "PROFILE",
                "userId": user_id,
                "channel": channel,
                "channelUserId": channel_user_id,
                "displayName": display_name or channel_user_id,
                "boundAt": now_iso,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Another invocation created it first — read and return theirs
            resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
            if "Item" in resp:
                return resp["Item"]["userId"], False
        logger.error("Failed to create channel mapping: %s", e)

    # User -> channel back-reference
    try:
        identity_table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"CHANNEL#{channel_key}",
                "channel": channel,
                "channelUserId": channel_user_id,
                "boundAt": now_iso,
            }
        )
    except ClientError:
        pass  # Non-critical

    logger.info("New user created: %s for %s", user_id, channel_key)
    return user_id, True


def get_or_create_session(user_id):
    """Get or create a session ID for the user. Session IDs must be >= 33 chars."""
    pk = f"USER#{user_id}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            # Update last activity
            identity_table.update_item(
                Key={"PK": pk, "SK": "SESSION"},
                UpdateExpression="SET lastActivity = :now",
                ExpressionAttributeValues={":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
            return resp["Item"]["sessionId"]
    except ClientError as e:
        logger.error("DynamoDB session lookup failed: %s", e)

    # Create new session (>= 33 chars required by AgentCore)
    session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
    if len(session_id) < 33:
        session_id += "_" + uuid.uuid4().hex[: 33 - len(session_id)]
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        identity_table.put_item(
            Item={
                "PK": pk,
                "SK": "SESSION",
                "sessionId": session_id,
                "createdAt": now_iso,
                "lastActivity": now_iso,
            }
        )
    except ClientError as e:
        logger.error("Failed to create session: %s", e)

    logger.info("New session created: %s for %s", session_id, user_id)
    return session_id


# ---------------------------------------------------------------------------
# Cross-channel binding
# ---------------------------------------------------------------------------

def create_bind_code(user_id):
    """Generate an 8-char bind code and store it in DynamoDB with TTL."""
    code = uuid.uuid4().hex[:8].upper()  # 16^8 = 4.3B keyspace
    ttl = int(time.time()) + BIND_CODE_TTL_SECONDS
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    identity_table.put_item(
        Item={
            "PK": f"BIND#{code}",
            "SK": "BIND",
            "userId": user_id,
            "createdAt": now_iso,
            "ttl": ttl,
        }
    )
    return code


def redeem_bind_code(code, channel, channel_user_id, display_name=""):
    """Redeem a bind code to link a new channel identity to an existing user.

    Returns (user_id, success).
    """
    code = code.strip().upper()
    try:
        resp = identity_table.get_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})
        item = resp.get("Item")
        if not item:
            return None, False
        # Check TTL (DynamoDB TTL deletion is eventual)
        if item.get("ttl", 0) < int(time.time()):
            return None, False

        user_id = item["userId"]
        channel_key = f"{channel}:{channel_user_id}"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Create channel -> user mapping
        identity_table.put_item(
            Item={
                "PK": f"CHANNEL#{channel_key}",
                "SK": "PROFILE",
                "userId": user_id,
                "channel": channel,
                "channelUserId": channel_user_id,
                "displayName": display_name or channel_user_id,
                "boundAt": now_iso,
            }
        )
        # Back-reference
        identity_table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"CHANNEL#{channel_key}",
                "channel": channel,
                "channelUserId": channel_user_id,
                "boundAt": now_iso,
            }
        )
        # Delete the bind code
        identity_table.delete_item(Key={"PK": f"BIND#{code}", "SK": "BIND"})

        logger.info("Bind code %s redeemed: %s -> %s", code, channel_key, user_id)
        return user_id, True
    except ClientError as e:
        logger.error("Bind code redemption failed: %s", e)
        return None, False


# ---------------------------------------------------------------------------
# AgentCore invocation
# ---------------------------------------------------------------------------

def invoke_agent_runtime(session_id, user_id, actor_id, channel, message):
    """Invoke the AgentCore Runtime with a per-user session.

    Message can be a plain string or a structured dict with text + images.
    """
    payload = json.dumps({
        "action": "chat",
        "userId": user_id,
        "actorId": actor_id,
        "channel": channel,
        "message": message,
    }).encode()

    try:
        logger.info("Invoking AgentCore: arn=%s qualifier=%s session=%s", AGENTCORE_RUNTIME_ARN, AGENTCORE_QUALIFIER, session_id)
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
        status_code = resp.get("statusCode")
        logger.info("AgentCore response status: %s", status_code)
        MAX_RESPONSE_BYTES = 500_000  # 500 KB — prevents OOM from large subagent responses
        body = resp.get("response")
        if body:
            if hasattr(body, "read"):
                body_bytes = body.read(MAX_RESPONSE_BYTES + 1)
                body_text = body_bytes.decode("utf-8", errors="replace")
                if len(body_bytes) > MAX_RESPONSE_BYTES:
                    logger.warning("AgentCore response truncated at %d bytes", MAX_RESPONSE_BYTES)
                    body_text = body_text[:MAX_RESPONSE_BYTES]
            else:
                body_text = str(body)[:MAX_RESPONSE_BYTES]
            logger.info("AgentCore response (len=%d, first 200 chars): %s", len(body_text), body_text[:200])
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                return {"response": body_text}
        logger.warning("AgentCore returned no response body")
        return {"response": "No response from agent."}
    except Exception as e:
        logger.error("AgentCore invocation failed: %s", e, exc_info=True)
        return {"response": f"Sorry, I'm having trouble right now. Please try again later."}


# ---------------------------------------------------------------------------
# Channel message senders
# ---------------------------------------------------------------------------

def _extract_text_from_content_blocks(text):
    """Extract plain text if the response is a JSON array of content blocks.

    AI responses sometimes arrive wrapped as: [{"type":"text","text":"..."}]
    The inner text values may contain literal newlines, so strict=False is
    required for the JSON decoder.

    Recursively unwraps nested content blocks — subagent responses can produce
    multiple layers of wrapping (e.g., subagent → parent agent → bridge).
    """
    if not text or not isinstance(text, str):
        return text
    result = text
    # Loop to unwrap multiple nesting levels (max 10 to prevent infinite loops)
    for _ in range(10):
        stripped = result.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            break
        try:
            blocks = json.JSONDecoder(strict=False).decode(stripped)
            if isinstance(blocks, list) and blocks:
                parts = [b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text"]
                if parts:
                    unwrapped = "".join(parts)
                    if unwrapped == result:
                        break  # No progress — avoid infinite loop
                    result = unwrapped
                    continue
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        break
    return result


def _markdown_to_telegram_html(text):
    """Convert common Markdown to Telegram-compatible HTML.

    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>,
    <a href="">, <blockquote>, <tg-spoiler>.

    Strategy: extract code blocks/inline code first (protect from other
    conversions), HTML-escape the rest, convert markdown patterns, then
    re-insert code.
    """
    if not text:
        return text

    placeholders = []

    def _placeholder(content):
        idx = len(placeholders)
        placeholders.append(content)
        return f"\x00PH{idx}\x00"

    # 1. Extract fenced code blocks: ```lang\n...\n```
    text = re.sub(
        r"```\w*\n?(.*?)```",
        lambda m: _placeholder(
            "<pre>{}</pre>".format(
                m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        ),
        text, flags=re.DOTALL,
    )

    # 2. Extract markdown tables and render as monospace <pre> blocks
    def _convert_table(m):
        lines = m.group(0).strip().split("\n")
        rows = []
        for line in lines:
            # Skip separator rows (|---|---|)
            stripped = line.strip().strip("|").strip()
            if stripped and not re.match(r"^[\s|:-]+$", stripped):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                rows.append(cells)
        if not rows:
            return m.group(0)
        # Calculate column widths
        col_count = max(len(r) for r in rows)
        widths = [0] * col_count
        for row in rows:
            for i, cell in enumerate(row):
                if i < col_count:
                    # Strip markdown bold for width calculation
                    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
                    widths[i] = max(widths[i], len(plain))
        # Format rows with padding
        formatted = []
        for ri, row in enumerate(rows):
            parts = []
            for i in range(col_count):
                cell = row[i] if i < len(row) else ""
                plain = re.sub(r"\*\*(.+?)\*\*", r"\1", cell)
                pad = widths[i] - len(plain) + len(cell)
                parts.append(cell.ljust(pad))
            formatted.append("  ".join(parts))
            # Add separator after header row
            if ri == 0:
                formatted.append("  ".join("─" * w for w in widths))
        table_text = "\n".join(formatted)
        table_text = table_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Convert bold inside table to HTML bold
        table_text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", table_text)
        return _placeholder(f"<pre>{table_text}</pre>")

    # Match consecutive lines that start with |
    text = re.sub(
        r"(?:^\|.+\|[ \t]*$\n?){2,}",
        _convert_table,
        text, flags=re.MULTILINE,
    )

    # 3. Extract inline code: `text`
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: _placeholder(
            "<code>{}</code>".format(
                m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        ),
        text,
    )

    # 4. HTML-escape remaining text
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 5. Convert markdown patterns to HTML

    # Headers: # Title → bold (Telegram has no header tag)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # Italic: *text* (but not bullet points like "* item")
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)

    # Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Links: [text](url) — allowlist safe URL schemes to prevent javascript:/data: injection
    def _safe_link(m):
        link_text, link_url = m.group(1), m.group(2)
        if re.match(r'^(https?://|tg://|mailto:)', link_url):
            return f'<a href="{link_url}">{link_text}</a>'
        return m.group(0)  # leave non-http links as plain text

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_link, text)

    # Blockquotes: > text (at line start)
    text = re.sub(r"^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    # Merge adjacent blockquotes into one
    text = text.replace("</blockquote>\n<blockquote>", "\n")

    # Horizontal rules: --- or === or *** → thin line
    text = re.sub(r"^[-=*]{3,}\s*$", "———", text, flags=re.MULTILINE)

    # 6. Re-insert placeholders
    for idx, content in enumerate(placeholders):
        text = text.replace(f"\x00PH{idx}\x00", content)

    return text


# ---------------------------------------------------------------------------
# Screenshot marker detection and delivery
# ---------------------------------------------------------------------------


def _extract_screenshots(text: str) -> tuple:
    """Extract [SCREENSHOT:key] markers from text.

    Returns (clean_text, [s3_keys]). The clean_text has all markers removed
    and extra whitespace stripped.
    """
    keys = SCREENSHOT_MARKER_RE.findall(text)
    clean = SCREENSHOT_MARKER_RE.sub("", text).strip()
    return clean, keys


def _fetch_s3_image(s3_key: str, namespace: str):
    """Fetch image bytes from S3. Returns None on error or invalid key."""
    # Validate: reject path traversal
    if ".." in s3_key:
        logger.error("Rejected S3 screenshot key with path traversal: %s", s3_key)
        return None
    # Validate: key must be within user namespace _screenshots/ prefix
    expected_prefix = f"{namespace}/_screenshots/"
    if not s3_key.startswith(expected_prefix):
        logger.error("Rejected S3 screenshot key outside user namespace: %s (expected prefix: %s)", s3_key, expected_prefix)
        return None
    try:
        bucket = os.environ["S3_USER_FILES_BUCKET"]
        resp = s3_client.get_object(Bucket=bucket, Key=s3_key)
        return resp["Body"].read()
    except Exception as e:
        logger.error("Failed to fetch screenshot from S3 key %s: %s", s3_key, e)
        return None


def _send_telegram_photo(chat_id: str, image_bytes: bytes, caption, token: str) -> bool:
    """Send a photo to Telegram chat via multipart form data. Returns True on success."""
    boundary = "----FormBoundary" + str(int(time.time()))
    parts = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{chat_id}"
    )
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="caption"\r\n\r\n'
            f"{caption}"
        )
    # Build body: text parts + binary photo part
    text_body = "\r\n".join(parts) + "\r\n"
    photo_header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="screenshot.png"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    )
    closing = f"\r\n--{boundary}--\r\n"
    body = text_body.encode() + photo_header.encode() + image_bytes + closing.encode()

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib_request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib_request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram photo: %s", e)
        return False


def _send_slack_file(channel_id: str, image_bytes: bytes, bot_token: str) -> bool:
    """Upload a screenshot to Slack using the v2 file upload API.

    Requires files:write Slack bot scope for screenshot delivery.
    """
    import urllib.request
    import urllib.parse

    try:
        # Step 1: Get upload URL
        params = urllib.parse.urlencode({"filename": "screenshot.png", "length": len(image_bytes)})
        req = urllib.request.Request(
            f"https://slack.com/api/files.getUploadURLExternal?{params}",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not resp.get("ok"):
            logger.error("Slack getUploadURLExternal failed: %s", resp.get("error"))
            return False

        upload_url = resp["upload_url"]
        file_id = resp["file_id"]

        # Step 2: Upload file bytes
        urllib.request.urlopen(
            urllib.request.Request(upload_url, data=image_bytes, method="POST"),
            timeout=30,
        )

        # Step 3: Complete upload and share to channel
        complete_data = json.dumps({
            "files": [{"id": file_id}],
            "channel_id": channel_id,
        }).encode()
        complete_req = urllib.request.Request(
            "https://slack.com/api/files.completeUploadExternal",
            data=complete_data,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
        )
        complete_resp = json.loads(urllib.request.urlopen(complete_req, timeout=10).read())
        return complete_resp.get("ok", False)
    except Exception as e:
        logger.error("Failed to send Slack file: %s", e)
        return False


def send_telegram_message(chat_id, text, token):
    """Send a message via Telegram Bot API.

    Converts Markdown to Telegram HTML for rich formatting. Falls back to
    plain text if Telegram rejects the HTML (e.g., malformed tags).
    """
    if not token:
        logger.error("No Telegram token available")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Try with HTML (converted from Markdown)
    html_text = _markdown_to_telegram_html(text)
    data = json.dumps({
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
        return
    except Exception as e:
        logger.warning("Telegram HTML send failed (retrying as plain text): %s", e)

    # Fallback: send as plain text (no parse_mode)
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
    }).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, e)


def send_telegram_typing(chat_id, token):
    """Send a typing indicator via Telegram Bot API."""
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/sendChatAction"
    data = json.dumps({"chat_id": chat_id, "action": "typing"}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=5)
    except Exception:
        pass


def _periodic_typing(chat_id, token, stop_event, interval=4, notify_after_s=30):
    """Send typing indicator every `interval` seconds until stop_event is set.

    Runs in a background thread. Telegram typing indicators expire after ~5s,
    so we send every 4s to keep the indicator visible during long AgentCore calls.

    After `notify_after_s` seconds, sends a one-time progress message so the user
    knows the bot is still working (e.g. during subagent tasks).
    """
    notified = False
    elapsed = 0
    while not stop_event.wait(timeout=interval):
        elapsed += interval
        send_telegram_typing(chat_id, token)
        if not notified and elapsed >= notify_after_s:
            send_telegram_message(
                chat_id,
                "\u23f3 Working on your request \u2014 this may take a few minutes. "
                "I'll send the full response when it's ready.",
                token,
            )
            notified = True


def send_slack_message(channel_id, text, bot_token):
    """Send a message via Slack Web API."""
    if not bot_token:
        logger.error("No Slack bot token available")
        return
    url = "https://slack.com/api/chat.postMessage"
    data = json.dumps({
        "channel": channel_id,
        "text": text,
    }).encode()
    req = urllib_request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bot_token}",
        },
    )
    try:
        urllib_request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("Failed to send Slack message to %s: %s", channel_id, e)


def _slack_progress_notify(channel_id, bot_token, stop_event, notify_after_s=30):
    """Send a one-time progress message to Slack if the request takes longer than notify_after_s."""
    if not stop_event.wait(timeout=notify_after_s):
        send_slack_message(
            channel_id,
            "\u23f3 Working on your request \u2014 this may take a few minutes. "
            "I'll send the full response when it's ready.",
            bot_token,
        )


# ---------------------------------------------------------------------------
# Image upload helpers
# ---------------------------------------------------------------------------

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 3_750_000  # 3.75 MB — Bedrock Converse limit
CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _upload_image_to_s3(image_bytes, namespace, content_type):
    """Upload image bytes to S3 and return the S3 key, or None on failure.

    S3 key: {namespace}/_uploads/img_{timestamp}_{hex}.{ext}
    """
    if not USER_FILES_BUCKET:
        logger.warning("USER_FILES_BUCKET not configured — cannot upload image")
        return None
    if content_type not in ALLOWED_IMAGE_TYPES:
        logger.warning("Rejected image with unsupported content type: %s", content_type)
        return None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logger.warning("Rejected image: %d bytes exceeds limit of %d", len(image_bytes), MAX_IMAGE_BYTES)
        return None

    ext = CONTENT_TYPE_TO_EXT.get(content_type, "bin")
    timestamp = int(time.time())
    hex_suffix = uuid.uuid4().hex[:8]
    s3_key = f"{namespace}/_uploads/img_{timestamp}_{hex_suffix}.{ext}"

    try:
        s3_client.put_object(
            Bucket=USER_FILES_BUCKET,
            Key=s3_key,
            Body=image_bytes,
            ContentType=content_type,
        )
        logger.info("Uploaded image to s3://%s/%s (%d bytes)", USER_FILES_BUCKET, s3_key, len(image_bytes))
        return s3_key
    except Exception as e:
        logger.error("S3 image upload failed: %s", e)
        return None


def _download_telegram_image(message, token):
    """Download the best-resolution photo or image document from a Telegram message.

    Returns (bytes, content_type, filename) or (None, None, None).
    """
    file_id = None
    content_type = "image/jpeg"  # Telegram photos are always JPEG

    # Check photo array (take last = highest resolution)
    photos = message.get("photo")
    if photos and isinstance(photos, list):
        file_id = photos[-1].get("file_id")

    # Check document with image mime type
    if not file_id:
        doc = message.get("document", {})
        mime = doc.get("mime_type", "")
        if mime in ALLOWED_IMAGE_TYPES:
            file_id = doc.get("file_id")
            content_type = mime

    if not file_id:
        return None, None, None

    try:
        # Get file path from Telegram API
        safe_file_id = quote(str(file_id), safe="")
        url = f"https://api.telegram.org/bot{token}/getFile?file_id={safe_file_id}"
        req = urllib_request.Request(url)
        resp = urllib_request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))
        file_path = data.get("result", {}).get("file_path", "")
        if not file_path:
            logger.warning("Telegram getFile returned no file_path for file_id=%s", file_id)
            return None, None, None

        # Check file size before downloading (Telegram includes it)
        file_size = data.get("result", {}).get("file_size", 0)
        if file_size > MAX_IMAGE_BYTES:
            logger.warning("Telegram file too large: %d bytes", file_size)
            return None, None, None

        # Download the file
        download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        req = urllib_request.Request(download_url)
        resp = urllib_request.urlopen(req, timeout=15)
        image_bytes = resp.read()

        filename = file_path.split("/")[-1] if "/" in file_path else file_path
        return image_bytes, content_type, filename
    except Exception as e:
        logger.error("Telegram image download failed: %s", e)
        return None, None, None


def _download_slack_file(file_info, bot_token):
    """Download an image file from Slack.

    Returns (bytes, content_type, filename) or (None, None, None).
    """
    mimetype = file_info.get("mimetype", "")
    if mimetype not in ALLOWED_IMAGE_TYPES:
        return None, None, None

    file_size = file_info.get("size", 0)
    if file_size > MAX_IMAGE_BYTES:
        logger.warning("Slack file too large: %d bytes", file_size)
        return None, None, None

    download_url = file_info.get("url_private_download") or file_info.get("url_private")
    if not download_url:
        logger.warning("Slack file has no download URL")
        return None, None, None

    try:
        req = urllib_request.Request(
            download_url,
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        resp = urllib_request.urlopen(req, timeout=15)
        image_bytes = resp.read()
        filename = file_info.get("name", "image")
        return image_bytes, mimetype, filename
    except Exception as e:
        logger.error("Slack file download failed: %s", e)
        return None, None, None


def _build_structured_message(text, s3_key, content_type):
    """Build a structured message dict with text and image reference."""
    return {
        "text": text or "",
        "images": [{"s3Key": s3_key, "contentType": content_type}],
    }


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------

def _is_bind_command(text):
    """Check if the message is a bind-code command (e.g. 'link ABCD1234')."""
    if not text:
        return False, ""
    parts = text.strip().split()
    if len(parts) == 2 and parts[0].lower() in ("link", "bind"):
        code = parts[1].strip().upper()
        if len(code) == 8 and code.isalnum():
            return True, code
    return False, ""


def _is_link_command(text):
    """Check if the message is a 'link accounts' command."""
    if not text:
        return False
    return text.strip().lower() in ("link accounts", "link account", "link")


def handle_telegram(body):
    """Process a Telegram webhook update."""
    update = json.loads(body) if isinstance(body, str) else body
    message = update.get("message", {})
    text = message.get("text", "") or message.get("caption", "")
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    user_id_tg = str(user.get("id", ""))
    display_name = user.get("first_name", "") or user.get("username", "")

    # Detect image: photo array or document with image mime type
    has_image = bool(
        message.get("photo")
        or (message.get("document", {}).get("mime_type", "") in ALLOWED_IMAGE_TYPES)
    )

    if not chat_id or not user_id_tg or (not text and not has_image):
        logger.info("Telegram: ignoring non-text/non-image or missing-user message")
        return

    if len(user_id_tg) > 128:
        logger.warning("Telegram channel_user_id too long (%d chars), rejecting", len(user_id_tg))
        return

    token = _get_telegram_token()

    # Handle bind commands BEFORE allowlist check — cross-channel binding
    # bypasses the allowlist since it links to an already-approved user.
    actor_id = f"telegram:{user_id_tg}"
    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user_id, success = redeem_bind_code(code, "telegram", user_id_tg, display_name)
        if success:
            send_telegram_message(chat_id, "Accounts linked successfully! Your sessions are now unified.", token)
        else:
            send_telegram_message(chat_id, "Invalid or expired link code. Please try again.", token)
        return

    # Resolve user identity
    resolved_user_id, is_new = resolve_user("telegram", user_id_tg, display_name)

    if resolved_user_id is None:
        send_telegram_message(
            chat_id,
            f"Sorry, this bot is private and requires an invitation.\n\n"
            f"Your ID: `telegram:{user_id_tg}`\n\n"
            f"Send this ID to the bot admin to request access.",
            token,
        )
        return

    # Handle link-accounts command (generate bind code for existing users)
    if _is_link_command(text):
        code = create_bind_code(resolved_user_id)
        send_telegram_message(
            chat_id,
            f"Your link code is: `{code}`\n\nEnter this code on another channel within 10 minutes "
            f"by typing: `link {code}`",
            token,
        )
        return

    # Send typing indicator
    send_telegram_typing(chat_id, token)

    # Build message payload (structured if image, plain string if text-only)
    agent_message = text
    if has_image:
        namespace = actor_id.replace(":", "_")
        image_bytes, content_type, _ = _download_telegram_image(message, token)
        if image_bytes:
            s3_key = _upload_image_to_s3(image_bytes, namespace, content_type)
            if s3_key:
                agent_message = _build_structured_message(text, s3_key, content_type)
            else:
                send_telegram_message(chat_id, "Sorry, I couldn't process that image. Please try again.", token)
                return
        else:
            send_telegram_message(chat_id, "Sorry, I couldn't download that image. Please try again.", token)
            return

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    image_count = 0 if isinstance(agent_message, str) else len(agent_message.get("images", []))
    logger.info(
        "Telegram: user=%s actor=%s session=%s text_len=%d images=%d",
        resolved_user_id, actor_id, session_id, len(text), image_count,
    )

    # Invoke AgentCore with periodic typing indicator
    stop_typing = threading.Event()
    typing_thread = threading.Thread(
        target=_periodic_typing,
        args=(chat_id, token, stop_typing),
        daemon=True,
    )
    typing_thread.start()
    try:
        result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, "telegram", agent_message)
    finally:
        stop_typing.set()
        typing_thread.join(timeout=2)
    logger.info("AgentCore result keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    # Extract plain text from content blocks if the contract server returned them raw
    response_text = _extract_text_from_content_blocks(response_text)
    logger.info("Response to send (len=%d): %s", len(response_text), response_text[:2000])

    # Extract and deliver screenshot images before sending text
    namespace = actor_id.replace(":", "_")
    response_text, screenshot_keys = _extract_screenshots(response_text)
    for s3_key in screenshot_keys:
        img_bytes = _fetch_s3_image(s3_key, namespace)
        if img_bytes:
            _send_telegram_photo(chat_id, img_bytes, None, token)
        else:
            logger.warning("Skipping undeliverable screenshot: %s", s3_key)

    # Send response (split if > 4096 chars for Telegram limit); skip if empty after stripping
    if response_text:
        if len(response_text) <= 4096:
            send_telegram_message(chat_id, response_text, token)
        else:
            for i in range(0, len(response_text), 4096):
                send_telegram_message(chat_id, response_text[i:i + 4096], token)
    logger.info("Telegram response sent to chat_id=%s (screenshots=%d)", chat_id, len(screenshot_keys))


def handle_slack(body, headers=None):
    """Process a Slack Events API webhook.

    Returns a response dict for immediate replies (url_verification).
    """
    event_data = json.loads(body) if isinstance(body, str) else body

    # Slack URL verification challenge
    if event_data.get("type") == "url_verification":
        return {"statusCode": 200, "body": json.dumps({"challenge": event_data["challenge"]})}

    # Ignore retries (Slack resends if no ACK within 3s — we self-invoke async)
    if headers and headers.get("x-slack-retry-num"):
        logger.info("Slack: ignoring retry %s", headers.get("x-slack-retry-num"))
        return {"statusCode": 200, "body": "ok"}

    event = event_data.get("event", {})
    # Allow "file_share" subtype (image uploads) in addition to plain messages
    if event.get("type") != "message" or event.get("subtype") not in (None, "file_share"):
        return {"statusCode": 200, "body": "ok"}

    text = event.get("text", "")
    slack_user_id = event.get("user", "")
    channel_id = event.get("channel", "")

    # Detect image files attached to the message (strict allowed-type check)
    image_files = [
        f for f in (event.get("files") or [])
        if f.get("mimetype", "") in ALLOWED_IMAGE_TYPES
    ]
    has_image = bool(image_files)

    if not slack_user_id or not channel_id or (not text and not has_image):
        return {"statusCode": 200, "body": "ok"}

    if len(slack_user_id) > 128:
        logger.warning("Slack channel_user_id too long (%d chars), rejecting", len(slack_user_id))
        return {"statusCode": 400, "body": "Invalid user ID"}

    # Ignore bot messages
    if event.get("bot_id"):
        return {"statusCode": 200, "body": "ok"}

    bot_token, _ = _get_slack_tokens()

    # Handle bind commands BEFORE allowlist check — cross-channel binding
    # bypasses the allowlist since it links to an already-approved user.
    actor_id = f"slack:{slack_user_id}"
    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user_id, success = redeem_bind_code(code, "slack", slack_user_id)
        if success:
            send_slack_message(channel_id, "Accounts linked successfully! Your sessions are now unified.", bot_token)
        else:
            send_slack_message(channel_id, "Invalid or expired link code. Please try again.", bot_token)
        return {"statusCode": 200, "body": "ok"}

    # Resolve user identity
    resolved_user_id, is_new = resolve_user("slack", slack_user_id)

    if resolved_user_id is None:
        send_slack_message(
            channel_id,
            f"Sorry, this bot is private and requires an invitation.\n\n"
            f"Your ID: `slack:{slack_user_id}`\n\n"
            f"Send this ID to the bot admin to request access.",
            bot_token,
        )
        return {"statusCode": 200, "body": "ok"}

    # Handle link-accounts command (generate bind code for existing users)
    if _is_link_command(text):
        code = create_bind_code(resolved_user_id)
        send_slack_message(
            channel_id,
            f"Your link code is: `{code}`\n\nEnter this code on another channel within 10 minutes "
            f"by typing: `link {code}`",
            bot_token,
        )
        return {"statusCode": 200, "body": "ok"}

    # Build message payload (structured if image, plain string if text-only)
    agent_message = text
    if has_image:
        namespace = actor_id.replace(":", "_")
        # Use first image file
        file_info = image_files[0]
        image_bytes, content_type, _ = _download_slack_file(file_info, bot_token)
        if image_bytes:
            s3_key = _upload_image_to_s3(image_bytes, namespace, content_type)
            if s3_key:
                agent_message = _build_structured_message(text, s3_key, content_type)
            else:
                send_slack_message(channel_id, "Sorry, I couldn't process that image. Please try again.", bot_token)
                return {"statusCode": 200, "body": "ok"}
        else:
            send_slack_message(channel_id, "Sorry, I couldn't download that image. Please try again.", bot_token)
            return {"statusCode": 200, "body": "ok"}

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    logger.info(
        "Slack: user=%s actor=%s session=%s msg_len=%d has_image=%s",
        resolved_user_id, actor_id, session_id, len(text), has_image,
    )

    # Invoke AgentCore with progress notification for long requests
    stop_notify = threading.Event()
    notify_thread = threading.Thread(
        target=_slack_progress_notify,
        args=(channel_id, bot_token, stop_notify),
        daemon=True,
    )
    notify_thread.start()
    try:
        result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, "slack", agent_message)
    finally:
        stop_notify.set()
        notify_thread.join(timeout=2)
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    response_text = _extract_text_from_content_blocks(response_text)

    # Extract and deliver screenshot images before sending text
    namespace = actor_id.replace(":", "_")
    response_text, screenshot_keys = _extract_screenshots(response_text)
    for s3_key in screenshot_keys:
        img_bytes = _fetch_s3_image(s3_key, namespace)
        if img_bytes:
            _send_slack_file(channel_id, img_bytes, bot_token)
        else:
            logger.warning("Skipping undeliverable screenshot: %s", s3_key)

    # Send text response; skip if empty after stripping markers
    if response_text:
        send_slack_message(channel_id, response_text, bot_token)
    return {"statusCode": 200, "body": "ok"}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """Lambda handler (API Gateway HTTP API) with async self-invocation for long processing."""
    # Check if this is an async self-invocation (already dispatched)
    if event.get("_async_dispatch"):
        channel = event.get("_channel")
        body = event.get("_body")
        headers = event.get("_headers", {})

        if channel == "telegram":
            handle_telegram(body)
        elif channel == "slack":
            handle_slack(body, headers)
        return {"statusCode": 200, "body": "ok"}

    # --- Function URL entry point ---
    request_context = event.get("requestContext", {})
    http_info = request_context.get("http", {})
    method = http_info.get("method", "")
    path = http_info.get("path", event.get("rawPath", ""))

    # Health check
    if method == "GET" and path == "/health":
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "ok", "service": "openclaw-router"}),
        }

    if method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")

    headers = event.get("headers", {})

    # Determine channel from path
    if path.endswith("/webhook/telegram"):
        # Validate webhook secret before processing
        if not validate_telegram_webhook(headers):
            logger.warning("Telegram webhook validation failed from %s", http_info.get("sourceIp", "unknown"))
            return {"statusCode": 401, "body": "Unauthorized"}

        # Self-invoke async and return immediately
        _self_invoke_async("telegram", body, headers)
        return {"statusCode": 200, "body": "ok"}

    elif path.endswith("/webhook/slack"):
        # Slack url_verification — only allowed during initial setup
        try:
            event_data = json.loads(body) if isinstance(body, str) else body
            if event_data.get("type") == "url_verification":
                if os.environ.get("SLACK_VERIFIED") == "true":
                    logger.warning("Slack url_verification rejected — already verified")
                    return {"statusCode": 403, "body": "Already verified"}
                # Validate challenge format before echoing (prevent injection)
                challenge = str(event_data.get("challenge", ""))
                if not re.match(r'^[a-zA-Z0-9_\-\.]{1,100}$', challenge):
                    return {"statusCode": 400, "body": "Invalid challenge format"}
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"challenge": challenge}),
                }
        except (json.JSONDecodeError, TypeError):
            pass

        # Validate Slack request signature before processing
        if not validate_slack_webhook(headers, body):
            logger.warning("Slack webhook validation failed from %s", http_info.get("sourceIp", "unknown"))
            return {"statusCode": 401, "body": "Unauthorized"}

        # Ignore Slack retries
        if headers.get("x-slack-retry-num"):
            return {"statusCode": 200, "body": "ok"}

        # Self-invoke async for actual processing
        _self_invoke_async("slack", body, headers)
        return {"statusCode": 200, "body": "ok"}

    return {"statusCode": 404, "body": "Not found"}


def _self_invoke_async(channel, body, headers):
    """Invoke this Lambda asynchronously to process the webhook in the background."""
    try:
        lambda_client.invoke(
            FunctionName=LAMBDA_FUNCTION_NAME,
            InvocationType="Event",  # async
            Payload=json.dumps({
                "_async_dispatch": True,
                "_channel": channel,
                "_body": body,
                "_headers": {k: v for k, v in (headers or {}).items()
                             if k.startswith("x-slack-")},
            }).encode(),
        )
    except Exception as e:
        logger.error("Self-invoke failed: %s", e, exc_info=True)
        # Do NOT fall back to synchronous processing — it could cause webhook
        # timeouts and the user's message will appear lost. The message is
        # already ACK'd to the platform; log the error for investigation.
