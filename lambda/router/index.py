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
import time
import uuid
from urllib import request as urllib_request

import boto3
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

# --- Clients (lazy init on cold start) ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

# --- Token cache (survives across warm invocations) ---
_token_cache = {}

BIND_CODE_TTL_SECONDS = 600  # 10 minutes


def _get_secret(secret_id):
    """Fetch a secret value, cached for the lifetime of the Lambda container."""
    if secret_id in _token_cache:
        return _token_cache[secret_id]
    if not secret_id:
        return ""
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        value = resp["SecretString"]
        _token_cache[secret_id] = value
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

def resolve_user(channel, channel_user_id, display_name=""):
    """Look up or create a user for the given channel identity.

    Returns (user_id, is_new).
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

    # 2. Create new user (conditional write to handle race conditions)
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
    """Generate a 6-char bind code and store it in DynamoDB with TTL."""
    code = uuid.uuid4().hex[:6].upper()
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
    """Invoke the AgentCore Runtime with a per-user session."""
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
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
        status_code = resp.get("statusCode")
        logger.info("AgentCore response status: %s", status_code)
        body = resp.get("response")
        if body:
            if hasattr(body, "read"):
                body_text = body.read().decode("utf-8")
            else:
                body_text = str(body)
            logger.info("AgentCore response body (first 500 chars): %s", body_text[:500])
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
    """
    if not text or not isinstance(text, str):
        return text
    stripped = text.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return text
    try:
        blocks = json.JSONDecoder(strict=False).decode(stripped)
        if isinstance(blocks, list) and blocks:
            parts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text"]
            if parts:
                return "".join(parts)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return text


def send_telegram_message(chat_id, text, token):
    """Send a message via Telegram Bot API.

    Tries Markdown parse_mode first; falls back to plain text if Telegram
    rejects the message (e.g., unescaped special characters in AI responses).
    """
    if not token:
        logger.error("No Telegram token available")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Try with Markdown first
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
        return
    except Exception as e:
        logger.warning("Telegram Markdown send failed (retrying as plain text): %s", e)

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


# ---------------------------------------------------------------------------
# Webhook handlers
# ---------------------------------------------------------------------------

def _is_bind_command(text):
    """Check if the message is a bind-code command (e.g. 'link ABC123')."""
    if not text:
        return False, ""
    parts = text.strip().split()
    if len(parts) == 2 and parts[0].lower() in ("link", "bind"):
        code = parts[1].strip().upper()
        if len(code) == 6 and code.isalnum():
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
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {})
    user_id_tg = str(user.get("id", ""))
    display_name = user.get("first_name", "") or user.get("username", "")

    if not chat_id or not user_id_tg or not text:
        logger.info("Telegram: ignoring non-text or missing-user message")
        return

    token = _get_telegram_token()

    # Resolve user identity
    actor_id = f"telegram:{user_id_tg}"
    resolved_user_id, is_new = resolve_user("telegram", user_id_tg, display_name)

    # Handle bind commands
    if _is_link_command(text):
        code = create_bind_code(resolved_user_id)
        send_telegram_message(
            chat_id,
            f"Your link code is: `{code}`\n\nEnter this code on another channel within 10 minutes "
            f"by typing: `link {code}`",
            token,
        )
        return

    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user_id, success = redeem_bind_code(code, "telegram", user_id_tg, display_name)
        if success:
            send_telegram_message(chat_id, "Accounts linked successfully! Your sessions are now unified.", token)
        else:
            send_telegram_message(chat_id, "Invalid or expired link code. Please try again.", token)
        return

    # Send typing indicator
    send_telegram_typing(chat_id, token)

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    logger.info(
        "Telegram: user=%s actor=%s session=%s msg_len=%d",
        resolved_user_id, actor_id, session_id, len(text),
    )

    # Invoke AgentCore
    result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, "telegram", text)
    logger.info("AgentCore result keys: %s", list(result.keys()) if isinstance(result, dict) else type(result))
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    # Extract plain text from content blocks if the contract server returned them raw
    response_text = _extract_text_from_content_blocks(response_text)
    logger.info("Response to send (len=%d): %s", len(response_text), response_text[:200])

    # Send response (split if > 4096 chars for Telegram limit)
    if len(response_text) <= 4096:
        send_telegram_message(chat_id, response_text, token)
    else:
        for i in range(0, len(response_text), 4096):
            send_telegram_message(chat_id, response_text[i:i + 4096], token)
    logger.info("Telegram response sent to chat_id=%s", chat_id)


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
    if event.get("type") != "message" or event.get("subtype"):
        return {"statusCode": 200, "body": "ok"}

    text = event.get("text", "")
    slack_user_id = event.get("user", "")
    channel_id = event.get("channel", "")

    if not slack_user_id or not text or not channel_id:
        return {"statusCode": 200, "body": "ok"}

    # Ignore bot messages
    if event.get("bot_id"):
        return {"statusCode": 200, "body": "ok"}

    bot_token, _ = _get_slack_tokens()

    # Resolve user identity
    actor_id = f"slack:{slack_user_id}"
    resolved_user_id, is_new = resolve_user("slack", slack_user_id)

    # Handle bind commands
    if _is_link_command(text):
        code = create_bind_code(resolved_user_id)
        send_slack_message(
            channel_id,
            f"Your link code is: `{code}`\n\nEnter this code on another channel within 10 minutes "
            f"by typing: `link {code}`",
            bot_token,
        )
        return {"statusCode": 200, "body": "ok"}

    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user_id, success = redeem_bind_code(code, "slack", slack_user_id)
        if success:
            send_slack_message(channel_id, "Accounts linked successfully! Your sessions are now unified.", bot_token)
        else:
            send_slack_message(channel_id, "Invalid or expired link code. Please try again.", bot_token)
        return {"statusCode": 200, "body": "ok"}

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    logger.info(
        "Slack: user=%s actor=%s session=%s msg_len=%d",
        resolved_user_id, actor_id, session_id, len(text),
    )

    # Invoke AgentCore
    result = invoke_agent_runtime(session_id, resolved_user_id, actor_id, "slack", text)
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    response_text = _extract_text_from_content_blocks(response_text)

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
        # Slack requires immediate response for url_verification
        # (url_verification is not signed — it's part of initial app setup)
        try:
            event_data = json.loads(body) if isinstance(body, str) else body
            if event_data.get("type") == "url_verification":
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"challenge": event_data["challenge"]}),
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
