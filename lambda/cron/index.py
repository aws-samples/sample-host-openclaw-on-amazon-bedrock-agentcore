"""Cron Executor Lambda — Triggered by EventBridge Scheduler.

Receives a scheduled event payload, warms up the user's AgentCore session
if cold, sends the cron message, and delivers the response to the user's
channel (Telegram or Slack).
"""

import hashlib
import json
import logging
import os
import re
import time
import uuid
from urllib import request as urllib_request

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
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
LAMBDA_TIMEOUT_SECONDS = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "600"))

# --- Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(
        read_timeout=max(LAMBDA_TIMEOUT_SECONDS - 30, 60),
        connect_timeout=10,
        retries={"max_attempts": 0},
    ),
)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)

# --- Token cache (survives across warm invocations, 15-min TTL) ---
_SECRET_CACHE_TTL_SECONDS = 900  # 15 minutes
_token_cache = {}  # {secret_id: (value, fetched_at)}

# --- Constants ---
WARMUP_POLL_INTERVAL_SECONDS = 15
WARMUP_MAX_WAIT_SECONDS = 300


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
            runtimeUserId=actor_id,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
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
    """Extract plain text if the response is a JSON array of content blocks.

    Recursively unwraps nested content blocks — subagent responses can produce
    multiple layers of wrapping (e.g., subagent -> parent agent -> bridge).
    """
    if not text or not isinstance(text, str):
        return text
    result = text
    for _ in range(10):
        stripped = result.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            break
        try:
            blocks = json.JSONDecoder(strict=False).decode(stripped)
            if isinstance(blocks, list) and blocks:
                parts = [
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if parts:
                    unwrapped = "".join(parts)
                    if unwrapped == result:
                        break
                    result = unwrapped
                    continue
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        break
    # Regex fallback: handle cases where JSON parsing fails (encoding issues, etc.)
    stripped_result = result.strip()
    if stripped_result.startswith("[{") and '"type"' in stripped_result and '"text"' in stripped_result:
        match = re.search(r'[,{]\s*"text"\s*[,:]\s*"((?:[^"\\]|\\.)*)"', stripped_result)
        if match:
            try:
                candidate = json.loads('"' + match.group(1) + '"')
                if candidate and candidate != result:
                    result = candidate
            except (json.JSONDecodeError, ValueError):
                pass
    return result


def _tables_to_pre_ascii(text):
    """Convert markdown tables to Unicode box-drawing ASCII tables in <pre> blocks.

    Telegram's <pre> block uses monospace font, so tables render beautifully.

    Input:
      | Name | Description |
      |------|-------------|
      | foo  | bar baz     |
      | qux  | quux        |

    Output:
      <pre>
      ┌──────┬─────────────┐
      │ Name │ Description │
      ├──────┼─────────────┤
      │ foo  │ bar baz     │
      │ qux  │ quux        │
      └──────┴─────────────┘
      </pre>
    """
    if not text or '|' not in text:
        return text

    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2:
            table_lines = []
            while i < len(lines):
                tl = lines[i].strip()
                if tl.startswith('|') and tl.endswith('|'):
                    table_lines.append(tl)
                    i += 1
                else:
                    break

            header = None
            rows = []
            for tl in table_lines:
                if re.match(r'^\|[\s\-\:\|]+\|$', tl):
                    continue
                cols = [c.strip() for c in tl.strip('|').split('|')]
                if header is None:
                    header = cols
                else:
                    rows.append(cols)

            if not header:
                result.extend(table_lines)
                continue

            num_cols = max(len(header), max((len(r) for r in rows), default=0))
            header = (header + [''] * num_cols)[:num_cols]
            rows = [(r + [''] * num_cols)[:num_cols] for r in rows]

            col_widths = []
            for c in range(num_cols):
                w = max(
                    len(header[c]),
                    max((len(row[c]) for row in rows), default=0),
                    3,
                )
                col_widths.append(w)

            def make_row(cells, left='│', sep='│', right='│'):
                parts = [f' {cells[c]:<{col_widths[c]}} ' for c in range(num_cols)]
                return left + sep.join(parts) + right

            def make_sep(left, mid, right, fill='─'):
                parts = [fill * (w + 2) for w in col_widths]
                return left + mid.join(parts) + right

            table_str = '\n'.join([
                make_sep('┌', '┬', '┐'),
                make_row(header),
                make_sep('├', '┼', '┤'),
                *[make_row(row) for row in rows],
                make_sep('└', '┴', '┘'),
            ])

            table_str = table_str.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            result.append(f'<pre>{table_str}</pre>')
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def _markdown_to_telegram_html(text):
    """Convert common Markdown to Telegram-compatible HTML.

    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>,
    <a href="">, <blockquote>, <tg-spoiler>.

    Strategy: convert tables to <pre> box-drawing blocks, extract code blocks/inline code
    first (protect from other conversions), HTML-escape the rest, convert
    markdown patterns, then re-insert code.
    """
    if not text:
        return text

    # Convert markdown tables to Unicode box-drawing ASCII in <pre> blocks
    text = _tables_to_pre_ascii(text)

    placeholders = []

    def _placeholder(content):
        idx = len(placeholders)
        placeholders.append(content)
        return f"\x00PH{idx}\x00"

    # 0. Extract table <pre> blocks (already HTML-escaped by _tables_to_pre_ascii)
    text = re.sub(
        r"<pre>(.*?)</pre>",
        lambda m: _placeholder(f"<pre>{m.group(1)}</pre>"),
        text, flags=re.DOTALL,
    )

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

    # 2. Extract inline code: `text`
    text = re.sub(
        r"`([^`\n]+)`",
        lambda m: _placeholder(
            "<code>{}</code>".format(
                m.group(1).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        ),
        text,
    )

    # 3. HTML-escape remaining text
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 4. Convert markdown patterns to HTML
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # Links: [text](url) — allowlist safe URL schemes to prevent javascript:/data: injection
    def _safe_link(m):
        link_text, link_url = m.group(1), m.group(2)
        if re.match(r'^(https?://|tg://|mailto:)', link_url):
            return f'<a href="{link_url}">{link_text}</a>'
        return m.group(0)  # leave non-http links as plain text

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _safe_link, text)
    text = re.sub(r"^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE)
    text = text.replace("</blockquote>\n<blockquote>", "\n")
    text = re.sub(r"^[-=*]{3,}\s*$", "———", text, flags=re.MULTILINE)

    # 5. Re-insert placeholders
    for idx, content in enumerate(placeholders):
        text = text.replace(f"\x00PH{idx}\x00", content)

    return text


def send_telegram_message(chat_id, text, token):
    """Send a message via Telegram Bot API.

    Converts Markdown to Telegram HTML for rich formatting. Falls back to
    plain text if Telegram rejects the HTML.
    """
    if not token:
        logger.error("No Telegram token available")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"

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
        logger.warning("Telegram HTML send failed (retrying plain): %s", e)

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

    # Verify schedule ownership — cross-check DynamoDB CRON# record
    try:
        cron_record = identity_table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": f"CRON#{schedule_id}"}
        ).get("Item")
        if not cron_record:
            logger.error(
                "Schedule %s not owned by user %s — skipping execution",
                schedule_id, user_id,
            )
            return {
                "statusCode": 403,
                "body": "Schedule ownership verification failed",
            }
    except Exception as e:
        logger.error("Failed to verify schedule ownership: %s", e)
        return {
            "statusCode": 500,
            "body": "Schedule ownership verification error",
        }

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
