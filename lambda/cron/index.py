"""Cron Executor Lambda — Triggered by EventBridge Scheduler.

Receives a scheduled event payload, warms up the user's AgentCore session
if cold, sends the cron message, and delivers the response to the user's
channel (Telegram or Slack).
"""

import hashlib
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
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

# --- Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client("bedrock-agentcore", region_name=AWS_REGION)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

# --- Token cache (survives across warm invocations) ---
_token_cache = {}

# --- Constants ---
WARMUP_POLL_INTERVAL_SECONDS = 15
WARMUP_MAX_WAIT_SECONDS = 300


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
    """Return (bot_token, signing_secret) tuple from Slack secret."""
    raw = _get_secret(SLACK_TOKEN_SECRET_ID)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        return data.get("botToken", ""), data.get("signingSecret", "")
    except (json.JSONDecodeError, TypeError):
        return raw, ""


# ---------------------------------------------------------------------------
# DynamoDB session management (reuse pattern from router Lambda)
# ---------------------------------------------------------------------------

def get_or_create_session(user_id):
    """Get or create a session ID for the user. Session IDs must be >= 33 chars."""
    pk = f"USER#{user_id}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            identity_table.update_item(
                Key={"PK": pk, "SK": "SESSION"},
                UpdateExpression="SET lastActivity = :now",
                ExpressionAttributeValues={
                    ":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                },
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
# AgentCore invocation helpers
# ---------------------------------------------------------------------------

def invoke_agentcore(session_id, action, user_id, actor_id, channel, message=None):
    """Invoke AgentCore Runtime with the given action."""
    payload_dict = {
        "action": action,
        "userId": user_id,
        "actorId": actor_id,
        "channel": channel,
    }
    if message:
        payload_dict["message"] = message

    payload = json.dumps(payload_dict).encode()

    try:
        logger.info(
            "Invoking AgentCore: action=%s session=%s user=%s",
            action, session_id, user_id,
        )
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=session_id,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
        body = resp.get("response")
        if body:
            if hasattr(body, "read"):
                body_text = body.read().decode("utf-8")
            else:
                body_text = str(body)
            logger.info("AgentCore response (first 500): %s", body_text[:500])
            try:
                return json.loads(body_text)
            except json.JSONDecodeError:
                return {"response": body_text}
        return {"response": "No response from agent."}
    except Exception as e:
        logger.error("AgentCore invocation failed: %s", e, exc_info=True)
        return {"response": f"Agent invocation failed: {e}"}


def warmup_and_wait(session_id, user_id, actor_id, channel):
    """Send warmup action and poll until the container is ready.

    Returns True if the container is ready, False if warmup timed out.
    """
    start = time.time()
    while time.time() - start < WARMUP_MAX_WAIT_SECONDS:
        result = invoke_agentcore(session_id, "warmup", user_id, actor_id, channel)
        status = result.get("status", "")
        logger.info("Warmup status: %s (elapsed: %.0fs)", status, time.time() - start)

        if status == "ready":
            return True

        if status != "initializing":
            # Unexpected status — might already be running or encountered an error
            # Try sending the cron action anyway
            logger.warning("Unexpected warmup status: %s — proceeding", status)
            return True

        time.sleep(WARMUP_POLL_INTERVAL_SECONDS)

    logger.error("Warmup timed out after %ds", WARMUP_MAX_WAIT_SECONDS)
    return False


# ---------------------------------------------------------------------------
# Channel message senders (duplicated from router Lambda — small and stable)
# ---------------------------------------------------------------------------

def _extract_text_from_content_blocks(text):
    """Extract plain text if the response is a JSON array of content blocks."""
    if not text or not isinstance(text, str):
        return text
    stripped = text.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return text
    try:
        blocks = json.JSONDecoder(strict=False).decode(stripped)
        if isinstance(blocks, list) and blocks:
            parts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if parts:
                return "".join(parts)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return text


def send_telegram_message(chat_id, text, token):
    """Send a message via Telegram Bot API."""
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
        logger.warning("Telegram Markdown send failed (retrying plain): %s", e)

    # Fallback: plain text
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, e)


def send_slack_message(channel_id, text, bot_token):
    """Send a message via Slack Web API."""
    if not bot_token:
        logger.error("No Slack bot token available")
        return
    url = "https://slack.com/api/chat.postMessage"
    data = json.dumps({"channel": channel_id, "text": text}).encode()
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


def deliver_response(channel, channel_target, response_text):
    """Deliver a response to the user's channel."""
    response_text = _extract_text_from_content_blocks(response_text)

    if channel == "telegram":
        token = _get_telegram_token()
        if len(response_text) <= 4096:
            send_telegram_message(channel_target, response_text, token)
        else:
            for i in range(0, len(response_text), 4096):
                send_telegram_message(channel_target, response_text[i : i + 4096], token)
    elif channel == "slack":
        bot_token, _ = _get_slack_tokens()
        send_slack_message(channel_target, response_text, bot_token)
    else:
        logger.warning("Unknown channel type: %s", channel)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """Handle EventBridge Scheduler trigger.

    Expected payload:
    {
        "userId": "user_abc123",
        "actorId": "telegram:12345",
        "channel": "telegram",
        "channelTarget": "12345",
        "message": "Check my email",
        "scheduleId": "a1b2c3d4",
        "scheduleName": "Daily email check"
    }
    """
    logger.info("Cron event received: %s", json.dumps(event)[:1000])

    user_id = event.get("userId")
    actor_id = event.get("actorId")
    channel = event.get("channel")
    channel_target = event.get("channelTarget")
    message = event.get("message")
    schedule_id = event.get("scheduleId", "unknown")
    schedule_name = event.get("scheduleName", "")

    if not all([user_id, actor_id, channel, channel_target, message]):
        logger.error(
            "Missing required fields. userId=%s actorId=%s channel=%s target=%s msg=%s",
            user_id, actor_id, channel, channel_target, bool(message),
        )
        return {"statusCode": 400, "body": "Missing required fields"}

    logger.info(
        "Processing cron: schedule=%s user=%s channel=%s:%s",
        schedule_id, user_id, channel, channel_target,
    )

    # Phase 1: Get or create session
    session_id = get_or_create_session(user_id)

    # Phase 2: Warm up the container if cold
    warmup_ok = warmup_and_wait(session_id, user_id, actor_id, channel)
    if not warmup_ok:
        error_msg = (
            f"[Scheduled: {schedule_name or schedule_id}] "
            "Your scheduled task could not run because the agent failed to start. "
            "It will try again at the next scheduled time."
        )
        deliver_response(channel, channel_target, error_msg)
        return {"statusCode": 503, "body": "Warmup timeout"}

    # Phase 3: Execute the cron message
    cron_message = f"[Scheduled task: {schedule_name or schedule_id}] {message}"
    result = invoke_agentcore(session_id, "cron", user_id, actor_id, channel, cron_message)
    response_text = result.get("response", "No response from scheduled task.")

    # Phase 4: Deliver response to channel
    logger.info("Delivering response (len=%d) to %s:%s", len(response_text), channel, channel_target)
    deliver_response(channel, channel_target, response_text)

    logger.info("Cron execution complete: schedule=%s", schedule_id)
    return {"statusCode": 200, "body": "OK"}
