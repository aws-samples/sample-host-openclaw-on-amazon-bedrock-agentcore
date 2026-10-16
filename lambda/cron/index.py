"""Cron Executor Lambda — Triggered by EventBridge Scheduler.

Receives a scheduled event payload, warms up the user's AgentCore session
if cold, sends the cron message, and delivers the response to the user's
channel (Telegram, Slack or Feishu).
"""

import json
import logging
import os
import re
import time
import uuid
from urllib import request as urllib_request

import boto3
from botocore.config import Config
from runtime_routing import admit_route, is_unknown_outcome, resolve_route
import whatsapp
import line_channel
from runtime_enrollment import authorize_channel_actor, read_actor_owner
from runtime_invocation import RELEASE_RESERVE_SECONDS, InvocationTransportError, invoke_bounded

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
TELEGRAM_TOKEN_SECRET_ID = os.environ.get("TELEGRAM_TOKEN_SECRET_ID", "")
SLACK_TOKEN_SECRET_ID = os.environ.get("SLACK_TOKEN_SECRET_ID", "")
FEISHU_TOKEN_SECRET_ID = os.environ.get("FEISHU_TOKEN_SECRET_ID", "")
WHATSAPP_TOKEN_SECRET_ID = os.environ.get("WHATSAPP_TOKEN_SECRET_ID", "")
LINE_TOKEN_SECRET_ID = os.environ.get("LINE_TOKEN_SECRET_ID", "")
FEISHU_API_DOMAIN = os.environ.get("FEISHU_API_DOMAIN", "https://open.feishu.cn")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
LAMBDA_TIMEOUT_SECONDS = int(os.environ.get("LAMBDA_TIMEOUT_SECONDS", "600"))
USER_FILES_BUCKET = os.environ.get("USER_FILES_BUCKET", "")

# --- Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION,
                         config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 0}))
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION,
                         config=Config(connect_timeout=2, read_timeout=10, retries={"max_attempts": 2}))


def runtime_client_factory(*, connect_timeout, read_timeout):
    return boto3.client(
        "bedrock-agentcore", region_name=AWS_REGION,
        config=Config(connect_timeout=connect_timeout, read_timeout=read_timeout, retries={"max_attempts": 0}),
    )

# --- Token cache (survives across warm invocations, 15-min TTL) ---
_SECRET_CACHE_TTL_SECONDS = 900  # 15 minutes
_token_cache = {}  # {secret_id: (value, fetched_at)}

# --- Constants ---
WARMUP_POLL_INTERVAL_SECONDS = 15
WARMUP_MAX_WAIT_SECONDS = 300
# Exception text can name the endpoint URL, runtime ARN and account ID, so
# users only ever see these fixed messages; details stay in logs as a type.
INVOCATION_FAILED_RESPONSE = "Agent invocation failed."
CRON_FAILED_MESSAGE = (
    "Your scheduled task did not return a result this time. "
    "It will run again at the next scheduled time."
)


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
        logger.warning("Failed to fetch secret: %s", type(e).__name__)
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


def _get_feishu_credentials():
    """Return (app_id, app_secret) from the Feishu secret (same format as the router)."""
    raw = _get_secret(FEISHU_TOKEN_SECRET_ID)
    if not raw:
        return "", ""
    try:
        data = json.loads(raw)
        return data.get("appId", ""), data.get("appSecret", "")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "", ""


# Feishu tenant_access_token cache (2h TTL, refresh 5 min early)
_feishu_token_cache = {"token": "", "expires_at": 0}


def _get_feishu_tenant_token():
    """Get or refresh Feishu tenant_access_token (2h TTL, refresh 5 min early).

    Mirrors _get_feishu_tenant_token in the router Lambda.
    """
    if _feishu_token_cache["token"] and time.time() < _feishu_token_cache["expires_at"] - 300:
        return _feishu_token_cache["token"]

    app_id, app_secret = _get_feishu_credentials()
    if not app_id or not app_secret:
        logger.error("Feishu app_id/app_secret not configured")
        return ""

    url = f"{FEISHU_API_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
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
        logger.error("Feishu token error: code=%s msg=%s", result.get("code"), result.get("msg", ""))
    except Exception as e:
        logger.error("Failed to get Feishu tenant_access_token: %s", type(e).__name__)
    return ""


# ---------------------------------------------------------------------------
# DynamoDB session management (reuse pattern from router Lambda)
# ---------------------------------------------------------------------------

def get_or_create_session(user_id):
    """Resolve an explicitly enrolled V2 route; missing routes require reconciliation."""
    return resolve_route(identity_table, user_id)


def resolve_current_user_id(actor_id):
    """Resolve the current internal userId from actorId via CHANNEL# PROFILE lookup.

    The same Telegram/Slack user may have had multiple internal userIds over time
    (session rotation). This ensures cron always uses the same session as the
    user's active chat container rather than an old/stale session.

    Returns None if the canonical channel identity cannot be verified.
    """
    if not actor_id:
        return None
    try:
        resp = identity_table.get_item(
            Key={"PK": f"CHANNEL#{actor_id}", "SK": "PROFILE"}, ConsistentRead=True,
        )
        if "Item" in resp and "userId" in resp["Item"]:
            resolved = resp["Item"]["userId"]
            owner = read_actor_owner(identity_table, actor_id)
            if owner and owner["userId"] != resolved:
                return None
            logger.info("Resolved current userId=%s from actorId=%s", resolved, actor_id)
            return resolved
    except Exception as e:
        logger.warning("Failed to resolve userId from actorId %s: %s", actor_id, e)
    return None


# ---------------------------------------------------------------------------
# AgentCore invocation helpers
# ---------------------------------------------------------------------------

def invoke_agentcore(route, action, user_id, actor_id, channel, message=None, deadline=None, channel_target=None):
    """Invoke AgentCore Runtime with the given action."""
    payload_dict = {
        "action": action,
        "userId": user_id,
        "actorId": route.actor_id,
        "channelActorId": actor_id,
        "channel": channel,
        "channelTarget": str(channel_target) if channel_target is not None else actor_id.split(":", 1)[-1],
        **route.payload_context(),
    }
    if message:
        payload_dict["message"] = message
    if deadline is None:
        deadline = time.monotonic() + max(0, LAMBDA_TIMEOUT_SECONDS - 30)
    if action == "cron":
        # Tell the contract how long this Lambda will read the response, so a
        # turn queued behind another brief stops (or never starts) before we
        # give up, instead of running on with no listener.
        budget_ms = int((deadline - RELEASE_RESERVE_SECONDS - time.monotonic()) * 1000)
        payload_dict["callerBudgetMs"] = max(1, min(900_000, budget_ms))

    payload = json.dumps(payload_dict).encode()

    try:
        authorize_channel_actor(identity_table, route, user_id, actor_id, channel)
        with admit_route(identity_table, route, user_id, route.actor_id) as admission:
            logger.info(
                "Invoking AgentCore: action=%s session=%s user=%s generation=%s",
                action, route.session_id, user_id, route.generation,
            )
            status_code, body_bytes = invoke_bounded(
                runtime_client_factory,
                {**route.invocation(), "payload": payload, "contentType": "application/json", "accept": "application/json"},
                deadline,
            )
            if not body_bytes:
                raise InvocationTransportError("AgentCore Runtime response is missing")
            try:
                result = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InvocationTransportError("AgentCore Runtime response is not valid JSON") from error
            if not isinstance(result, dict):
                raise InvocationTransportError("AgentCore Runtime response is not an object")
            if is_unknown_outcome(result):
                admission.retain()
            if status_code != 200:
                return {"response": INVOCATION_FAILED_RESPONSE, "status": "failed"}
            return result
    except Exception as e:
        # Log the type only: botocore messages embed the runtime ARN and account ID.
        logger.error("AgentCore invocation failed: %s", type(e).__name__)
        return {"response": INVOCATION_FAILED_RESPONSE, "status": "failed"}


def warmup_and_wait(session_id, user_id, actor_id, channel, deadline=None, channel_target=None):
    """Send warmup action and poll until the container is ready.

    Returns True if the container is ready, False if warmup timed out.
    """
    start = time.monotonic()
    deadline = min(deadline or float("inf"), start + WARMUP_MAX_WAIT_SECONDS)
    while time.monotonic() < deadline:
        result = invoke_agentcore(session_id, "warmup", user_id, actor_id, channel, deadline=deadline,
                                  channel_target=channel_target)
        if (not isinstance(result, dict) or result.get("error") or is_unknown_outcome(result)
                or time.monotonic() >= deadline):
            return False
        status = result.get("status", "")
        phase = result.get("phase")
        logger.info("Warmup status: %s (elapsed: %.0fs)", status, time.monotonic() - start)

        if status == "ready":
            return phase in (None, "USER_READY")
        if phase not in (None, "BOOTSTRAPPING", "BOOTSTRAP_READY", "BINDING", "RECOVERING"):
            return False

        if status != "initializing":
            logger.warning("Unexpected warmup status: %s", status)
            return False

        time.sleep(min(WARMUP_POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))

    logger.error("Warmup timed out after %ds", WARMUP_MAX_WAIT_SECONDS)
    return False


# ---------------------------------------------------------------------------
# Channel message senders (duplicated from router Lambda — small and stable)
# ---------------------------------------------------------------------------

# _BLOCK_CLOSE_RE, _MALFORMED_TEXT_BLOCK_RE and _extract_text_from_content_blocks
# are copied from lambda/router/index.py (#118). Keep the two copies in sync.

# End of a (possibly malformed) content-block array: '}]' or '},]'.
_BLOCK_CLOSE_RE = re.compile(r'\}\s*,?\s*\]')
# Text value of a '{"type":"text","text":"..."}' block, for regex fallback.
# Accepts ',' as well as ':' before the value (seen from models in the wild).
_MALFORMED_TEXT_BLOCK_RE = re.compile(
    r'"type"\s*:\s*"text"\s*,\s*"text"\s*[,:]\s*"((?:[^"\\]|\\.)*)"'
)


def _extract_text_from_content_blocks(text):
    """Extract plain text from content blocks anywhere in the response.

    Handles three cases:
    1. Entire string is a JSON array: [{"type":"text","text":"..."}]
    2. Content blocks embedded in surrounding text: "prefix[{...}]suffix"
    3. Nested content blocks (subagent wrapping): recursively unwraps up to 10 levels

    Scans for '[{' positions and attempts JSON parse at each to handle
    complex nested/escaped content blocks from subagent responses.
    """
    if not text or not isinstance(text, str):
        return text
    result = text
    decoder = json.JSONDecoder(strict=False)
    for _ in range(10):
        prev = result
        # Scan for all '[{' positions and try to parse JSON arrays
        rebuilt = []
        i = 0
        while i < len(result):
            pos = result.find("[{", i)
            if pos == -1:
                rebuilt.append(result[i:])
                break
            rebuilt.append(result[i:pos])
            # Try to parse a JSON array starting at pos
            try:
                blocks, end = decoder.raw_decode(result, pos)
                if isinstance(blocks, list) and blocks and all(isinstance(b, dict) for b in blocks):
                    # Check if this looks like a content block array (dicts with "type" keys)
                    has_typed_blocks = any(b.get("type") for b in blocks)
                    if has_typed_blocks:
                        parts = [
                            b.get("text", "")
                            for b in blocks
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        # Always advance past the block — image-only blocks produce empty text
                        rebuilt.append("".join(parts))
                        i = end
                        continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            # Not a valid content block array. If it looks like content-block
            # JSON (e.g. '[{"type":' ...), only an unterminated fragment that
            # runs to the end of the string is dropped (a partial stream tail).
            # A closed but malformed array keeps the text around it.
            remainder = result[pos:]
            if re.match(r'^\[\{\s*"type"\s*:', remainder) or remainder.strip() == "[{":
                close = _BLOCK_CLOSE_RE.search(remainder)
                if close is None:
                    # Truncated content block JSON at the tail — skip the rest
                    break
                span = remainder[:close.end()]
                texts = _MALFORMED_TEXT_BLOCK_RE.findall(span)
                if texts:
                    try:
                        rebuilt.append("".join(json.loads('"' + t + '"') for t in texts))
                        i = pos + close.end()
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass
            rebuilt.append("[")
            i = pos + 1
        result = "".join(rebuilt)
        if result == prev:
            break
    return result


def _tables_to_bullets(text):
    """Convert markdown tables to bold-name bullet lists for Telegram.

    | Name | Description |       ->    • **Name** — Description
    |------|-------------|
    | foo  | bar         |       ->    • **foo** — bar

    Works with CJK characters and emoji (no alignment issues).
    Uses ** for bold so _markdown_to_telegram_html converts to <b> tags.
    """
    if not text or '|' not in text:
        return text

    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
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
            bullets = []
            for tl in table_lines:
                if re.match(r'^\|[\s\-\:\|]+\|$', tl):
                    continue
                cols = [c.strip() for c in tl.strip('|').split('|')]
                cols = [c for c in cols if c]
                if not cols:
                    continue
                if header is None:
                    header = cols
                    continue
                if len(cols) == 1:
                    bullets.append(f'\u2022 {cols[0]}')
                elif len(cols) >= 2:
                    name = cols[0]
                    desc = ' \u2014 '.join(cols[1:])
                    bullets.append(f'\u2022 **{name}** \u2014 {desc}')

            result.extend(bullets)
        else:
            result.append(lines[i])
            i += 1

    return '\n'.join(result)


def _markdown_to_telegram_html(text):
    """Convert common Markdown to Telegram-compatible HTML.

    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>,
    <a href="">, <blockquote>, <tg-spoiler>.

    Strategy: convert tables to bullet lists, extract code blocks/inline
    code (protect from other conversions), HTML-escape the rest, convert
    markdown patterns, then re-insert code.
    """
    if not text:
        return text

    # Convert markdown tables to bullet lists (uses ** for bold, converted below)
    text = _tables_to_bullets(text)

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
    plain text if Telegram rejects the HTML. Returns True if either send
    was accepted, False otherwise.
    """
    if not token:
        logger.error("No Telegram token available")
        return False
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
        return True
    except Exception as e:
        logger.warning("Telegram HTML send failed (retrying plain): %s", e)

    # Fallback: plain text
    data = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib_request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib_request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, e)
        return False


# Telegram's sendMessage limit is 4096 characters counted in UTF-16 code
# units: an emoji or other astral character counts 2. Chunks are cut below
# the limit to leave room for small growth in the Markdown->HTML conversion
# (e.g. table rows turned into bullets).
TELEGRAM_MAX_UTF16_UNITS = 4096
TELEGRAM_CHUNK_UTF16_UNITS = 4000


def _utf16_len(text):
    """Length of text as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def _best_break(window):
    """Index to cut window at: a paragraph, line or word boundary if one lies
    in the second half, preferring cuts outside a ``` fence; else the end."""
    fences = [m.start() for m in re.finditer(r"```", window)]
    floor = len(window) // 2

    def outside_fence(cut):
        return sum(1 for f in fences if f + 3 <= cut) % 2 == 0

    for require_fence_balance in (True, False):
        for sep in ("\n\n", "\n", " "):
            pos = window.rfind(sep)
            while pos >= 0 and pos + len(sep) > floor:
                cut = pos + len(sep)
                if not require_fence_balance or outside_fence(cut):
                    return cut
                pos = window.rfind(sep, 0, pos)
    return len(window)


def _split_telegram_text(text, limit=TELEGRAM_CHUNK_UTF16_UNITS):
    """Split text into chunks of at most `limit` UTF-16 code units.

    Cuts only between code points, so a surrogate pair is never split, and
    the chunks concatenate back to the original text.
    """
    chunks = []
    rest = text or ""
    while rest:
        if _utf16_len(rest) <= limit:
            chunks.append(rest)
            break
        units = 0
        fit = 0
        for ch in rest:
            units += 2 if ord(ch) > 0xFFFF else 1
            if units > limit:
                break
            fit += 1
        cut = _best_break(rest[:fit]) if fit else 1
        chunks.append(rest[:cut])
        rest = rest[cut:]
    return chunks


def _send_telegram_chunks(chat_id, text, token, limit=TELEGRAM_CHUNK_UTF16_UNITS, retry=True):
    """Send text as one or more Telegram messages within the UTF-16 limit.

    A chunk Telegram rejects is re-sent once as pieces of half the size; a
    chunk that still fails is logged as dropped. Returns True if every chunk
    was delivered.
    """
    chunks = _split_telegram_text(text, limit)
    delivered_all = True
    for n, chunk in enumerate(chunks, 1):
        if send_telegram_message(chat_id, chunk, token):
            continue
        size = _utf16_len(chunk)
        if retry and size > limit // 2:
            logger.warning(
                "Telegram chunk %d/%d (%d UTF-16 units) rejected; retrying in smaller pieces",
                n, len(chunks), size,
            )
            if _send_telegram_chunks(chat_id, chunk, token, limit // 2, retry=False):
                continue
        else:
            logger.error(
                "Dropped Telegram chunk %d/%d (%d UTF-16 units) for %s after send failed",
                n, len(chunks), size, chat_id,
            )
        delivered_all = False
    return delivered_all


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


def send_feishu_message(receiver_id, text):
    """Send a message via Feishu Bot API.

    receiver_id can be an open_id (ou_xxx) or chat_id (oc_xxx).
    The API receive_id_type is auto-detected from the prefix.
    """
    token = _get_feishu_tenant_token()
    if not token:
        logger.error("No Feishu tenant_access_token available")
        return

    id_type = "open_id" if receiver_id.startswith("ou_") else "chat_id"
    url = f"{FEISHU_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type={id_type}"
    MAX_FEISHU_TEXT_LEN = 20000

    chunks = [text[i:i + MAX_FEISHU_TEXT_LEN]
              for i in range(0, len(text), MAX_FEISHU_TEXT_LEN)] if len(text) > MAX_FEISHU_TEXT_LEN else [text]

    for chunk in chunks:
        data = json.dumps({
            "receive_id": receiver_id,
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
            logger.error("Failed to send Feishu message to %s: %s", receiver_id, e)



# ---------------------------------------------------------------------------
# Brief audio (BRIEF_AUDIO marker -> Telegram sendAudio)
# ---------------------------------------------------------------------------
#
# The agent can't attach audio to a cron reply: tts and read are denied, local
# media outside OpenClaw's roots is dropped, and the contract keeps only text.
# So a brief uploads its mp3 to the user's S3 namespace and ends the reply with
# one line "BRIEF_AUDIO: <namespace>/<file>.mp3". The Lambda strips that line
# and sends the object itself. The IAM grant allows any */*.mp3 object in the
# bucket, so the namespace check below is what keeps one user's brief from
# sending another user's file.
#
# The label must not look like a secret name. OpenClaw's gateway redacts the
# value after any upper-case label ending in KEY, TOKEN, SECRET, PASSWORD or
# PASSWD followed by ':' or '=' (2026.9.5 dist/redact-*.mjs,
# ENV_ASSIGNMENT_REDACT_PATTERN), and only scans text containing KEY, TOKEN,
# SECRET, PASSWORD, PASSWD, AUTH, COOKIE, SIGNATURE, CREDENTIAL, CARD, CVC, CVV
# or PAYMENT (DEFAULT_REDACT_PREFILTER_SOURCES, case-insensitive). The earlier
# label AUDIO_S3_KEY reached the Lambda as "AUDIO_S3_KEY: ***". BRIEF_AUDIO
# contains none of those words; bridge/brief-audio-marker.test.js checks it
# against OpenClaw's own redaction when the dist is installed.
AUDIO_MARKER_LABEL = "BRIEF_AUDIO"

# Telegram Bot API upload limit for sendAudio.
AUDIO_MAX_BYTES = 50 * 1024 * 1024
# Don't start the download/upload with less than this left before the deadline.
AUDIO_MIN_SECONDS = 10
AUDIO_UPLOAD_TIMEOUT_SECONDS = 60

# Any line that starts with the marker (Markdown bold/code around it allowed).
_AUDIO_MARKER_LINE_RE = re.compile(rf"^[\s`*]*{AUDIO_MARKER_LABEL}[\s`*]*:", re.MULTILINE)
_AUDIO_MARKER_RE = re.compile(rf"^[\s`*]*{AUDIO_MARKER_LABEL}[\s`*]*:[\s`*]*(?P<key>[^\s`*]*)[\s`*]*$")
# Same character set the s3-user-files skill's sanitize() produces, plus '/'.
_AUDIO_KEY_RE = re.compile(r"^[A-Za-z0-9_\-./]+$")


def split_audio_marker(text):
    """Remove every BRIEF_AUDIO line from text.

    Returns (text_without_marker_lines, key). key is the value of the last
    marker line, or None. The text is returned unchanged when there is no
    marker, so replies without one are delivered exactly as before. A marker
    line whose value can't be read (empty, redacted to ***, extra words) is
    still stripped and logged with a fixed warning; its text is never logged.
    """
    if not isinstance(text, str) or not _AUDIO_MARKER_LINE_RE.search(text):
        return text, None
    key = None
    kept = []
    for line in text.split("\n"):
        if _AUDIO_MARKER_LINE_RE.match(line):
            match = _AUDIO_MARKER_RE.match(line)
            key = (match.group("key") if match else "") or None
            continue
        kept.append(line)
    if key is None:
        logger.warning("Brief audio skipped: marker line present but its key could not be read")
    return "\n".join(kept).rstrip(), key


def _audio_namespace(actor_id):
    """User-files namespace for an actor, as the router and contract derive it."""
    return (actor_id or "").replace(":", "_")


def _valid_audio_key(key, namespace):
    if not isinstance(key, str) or not namespace or len(key) > 1024:
        return False
    if ".." in key or not _AUDIO_KEY_RE.match(key):
        return False
    prefix = f"{namespace}/"
    if not key.startswith(prefix) or not key.endswith(".mp3"):
        return False
    name = key.rsplit("/", 1)[-1]
    return len(name) > len(".mp3")


def _multipart_body(fields, file_field, filename, content_type, data):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def send_telegram_audio(chat_id, filename, data, token, timeout):
    """Upload an mp3 with Telegram sendAudio. Raises on failure."""
    body, content_type = _multipart_body(
        {"chat_id": chat_id}, "audio", filename, "audio/mpeg", data,
    )
    req = urllib_request.Request(
        f"https://api.telegram.org/bot{token}/sendAudio",
        data=body, headers={"Content-Type": content_type}, method="POST",
    )
    urllib_request.urlopen(req, timeout=timeout)


def deliver_brief_audio(channel, channel_target, key, actor_id, deadline):
    """Send the brief's mp3 from S3 after its text. Never raises.

    Only Telegram is supported. Logs carry a fixed message and the error type
    only: the key and target are not logged.
    """
    if not key:
        return False
    if channel != "telegram":
        logger.info("Brief audio skipped: channel %s has no audio delivery", channel)
        return False
    if not USER_FILES_BUCKET:
        logger.warning("Brief audio skipped: user files bucket not configured")
        return False
    if not _valid_audio_key(key, _audio_namespace(actor_id)):
        logger.warning("Brief audio skipped: key rejected (outside namespace, not .mp3, or unsafe)")
        return False
    remaining = deadline - time.monotonic()
    if remaining < AUDIO_MIN_SECONDS:
        logger.warning("Brief audio skipped: %d s left before the deadline", int(remaining))
        return False
    try:
        head = s3_client.head_object(Bucket=USER_FILES_BUCKET, Key=key)
        size = int(head.get("ContentLength", 0))
        if size <= 0 or size > AUDIO_MAX_BYTES:
            logger.warning("Brief audio skipped: object size %d outside 1..%d bytes", size, AUDIO_MAX_BYTES)
            return False
        data = s3_client.get_object(Bucket=USER_FILES_BUCKET, Key=key)["Body"].read(AUDIO_MAX_BYTES + 1)
        if len(data) > AUDIO_MAX_BYTES:
            logger.warning("Brief audio skipped: object grew past %d bytes", AUDIO_MAX_BYTES)
            return False
        token = _get_telegram_token()
        if not token:
            logger.warning("Brief audio skipped: no Telegram token available")
            return False
        timeout = max(1, min(AUDIO_UPLOAD_TIMEOUT_SECONDS, deadline - time.monotonic() - 2))
        send_telegram_audio(channel_target, key.rsplit("/", 1)[-1], data, token, timeout)
    except Exception as e:
        logger.warning("Brief audio not sent: %s", type(e).__name__)
        return False
    logger.info("Brief audio sent (%d bytes)", len(data))
    return True


def deliver_response(channel, channel_target, response_text):
    """Deliver a response to the user's channel."""
    response_text = _extract_text_from_content_blocks(response_text)
    # The audio marker is for the Lambda only; it never reaches the user.
    response_text, _ = split_audio_marker(response_text)

    if channel == "telegram":
        token = _get_telegram_token()
        if not token:
            logger.error("No Telegram token available; cron response not delivered")
            return
        if not _send_telegram_chunks(channel_target, response_text, token):
            logger.error("Cron response to Telegram %s was not fully delivered", channel_target)
    elif channel == "slack":
        bot_token, _ = _get_slack_tokens()
        send_slack_message(channel_target, response_text, bot_token)
    elif channel == "feishu":
        send_feishu_message(channel_target, response_text)
    elif channel == "whatsapp":
        # Free-form messages only reach users inside WhatsApp's 24-hour
        # customer-service window; outside it Meta rejects the send.
        config = whatsapp.parse_config(_get_secret(WHATSAPP_TOKEN_SECRET_ID)) if WHATSAPP_TOKEN_SECRET_ID else None
        if not config:
            logger.error("WhatsApp channel not configured; cron response not delivered")
            return
        if not whatsapp.send_text(config, channel_target, response_text):
            logger.error("Cron response to WhatsApp was not delivered (outside the 24h window?)")
    elif channel == "line":
        # Cron results go out through the push API (counts against the plan's message quota).
        config = line_channel.parse_config(_get_secret(LINE_TOKEN_SECRET_ID)) if LINE_TOKEN_SECRET_ID else None
        if not config:
            logger.error("LINE channel not configured; cron response not delivered")
            return
        if not line_channel.send_text(config, channel_target, response_text):
            logger.error("Cron response to LINE was not delivered")
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
    actor_id = event.get("channelActorId") or event.get("actorId")
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

    # Resolve current userId from actorId BEFORE ownership check.
    # The payload userId may be stale (from before userId was made deterministic).
    # Look up the current active userId via CHANNEL# PROFILE so the ownership check
    # uses the correct userId, and cron shares the same session as chat.
    current_user_id = resolve_current_user_id(actor_id)
    if not current_user_id or not actor_id.startswith(f"{channel}:"):
        return {"statusCode": 403, "body": "Channel identity verification failed"}
    if current_user_id != user_id:
        logger.info(
            "actorId=%s resolved to current userId=%s (payload had %s)",
            actor_id, current_user_id, user_id,
        )

    # Verify schedule ownership — cross-check DynamoDB CRON# record
    # Use current_user_id (resolved from channel identity) for the lookup.
    try:
        cron_record = identity_table.get_item(
            Key={"PK": f"USER#{current_user_id}", "SK": f"CRON#{schedule_id}"}, ConsistentRead=True,
        ).get("Item")
        if not cron_record:
            logger.error(
                "Schedule %s not owned by user %s — skipping execution",
                schedule_id, current_user_id,
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
    try:
        session_id = get_or_create_session(current_user_id)
        authorize_channel_actor(identity_table, session_id, current_user_id, actor_id, channel)
    except Exception as error:
        logger.error("AgentCore Runtime route unavailable: %s", error)
        return {"statusCode": 503, "body": "Runtime route unavailable"}

    # Phase 2: Warm up the container if cold
    remaining = context.get_remaining_time_in_millis() / 1000 if context else LAMBDA_TIMEOUT_SECONDS
    lambda_deadline = time.monotonic() + remaining
    warmup_ok = warmup_and_wait(
        session_id, current_user_id, actor_id, channel,
        deadline=lambda_deadline - 60, channel_target=channel_target,
    )
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
    if context:
        lambda_deadline = min(lambda_deadline, time.monotonic() + context.get_remaining_time_in_millis() / 1000)
    result = invoke_agentcore(
        session_id, "cron", current_user_id, actor_id, channel, cron_message,
        deadline=lambda_deadline - 15, channel_target=channel_target,
    )
    if (not isinstance(result, dict) or result.get("status") in ("failed", "error")
            or is_unknown_outcome(result) or result.get("error")):
        status = result.get("status") if isinstance(result, dict) else None
        logger.warning("Scheduled task %s did not complete: status=%s", schedule_id, status)
        # Never forward the runtime's response text on failure; send a fixed notice.
        deliver_response(channel, channel_target, f"[Scheduled: {schedule_name or schedule_id}] {CRON_FAILED_MESSAGE}")
        return {"statusCode": 503, "body": "Scheduled task invocation failed or outcome unknown"}
    response_text = result.get("response", "No response from scheduled task.")
    response_text, audio_key = split_audio_marker(_extract_text_from_content_blocks(response_text))

    # Phase 4: Deliver response to channel, then the brief's audio (if any)
    logger.info("Delivering response (len=%d) to %s:%s", len(response_text), channel, channel_target)
    deliver_response(channel, channel_target, response_text)
    if audio_key:
        deliver_brief_audio(channel, channel_target, audio_key, session_id.actor_id, lambda_deadline)

    logger.info("Cron execution complete: schedule=%s", schedule_id)
    return {"statusCode": 200, "body": "OK"}
