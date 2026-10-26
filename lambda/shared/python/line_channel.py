"""LINE Messaging API helpers for the Router and Cron Lambdas.

Stdlib only. The channel is disabled unless the ``openclaw/channels/line``
secret is a JSON object with ``channelSecret`` and ``channelAccessToken``; the
deploy-time placeholder (a random string) therefore leaves the endpoint closed.

Inbound:  POST /webhook/line, signed with X-Line-Signature
          (base64 HMAC-SHA256 of the raw body with the channel secret).
Outbound: reply API with the event's reply token, falling back to the push
          API when the token has expired (agent turns often take longer than
          the token lives).
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger()

API_BASE = "https://api.line.me/v2/bot"
DATA_API_BASE = "https://api-data.line.me/v2/bot"

_USER_ID_RE = re.compile(r"^U[0-9a-f]{32}$")
_MESSAGE_ID_RE = re.compile(r"^\d{1,32}$")
_EVENT_ID_RE = re.compile(r"^[0-9A-Za-z]{1,64}$")
_REPLY_TOKEN_RE = re.compile(r"^[0-9A-Za-z_\-]{1,256}$")
_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")

MAX_BODY_BYTES = 1_000_000
# LINE text messages are limited to 5000 characters; one request carries at most 5 messages.
TEXT_CHUNK_CHARS = 4900
MAX_MESSAGES_PER_REQUEST = 5
# LINE signs no timestamp, so the event's own timestamp bounds replay. Redelivered
# events (deliveryContext.isRedelivery, inside the signed body) get a longer window.
MAX_EVENT_AGE_SECONDS = 300
MAX_REDELIVERY_AGE_SECONDS = 3600


def parse_config(raw):
    """Return the channel config dict, or None when the channel is not configured."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    secret = data.get("channelSecret")
    token = data.get("channelAccessToken")
    if not isinstance(secret, str) or not secret or not isinstance(token, str) or not token:
        return None
    bot_user_id = data.get("botUserId") or ""
    if bot_user_id and (not isinstance(bot_user_id, str) or not _USER_ID_RE.match(bot_user_id)):
        return None
    return {"channelSecret": secret, "channelAccessToken": token, "botUserId": bot_user_id}


def verify_signature(headers, body_bytes, channel_secret):
    """Check X-Line-Signature = base64(HMAC-SHA256(channel_secret, raw body)). Fail-closed."""
    if not channel_secret:
        return False
    signature = (headers or {}).get("x-line-signature", "")
    if not isinstance(signature, str) or not _SIGNATURE_RE.match(signature):
        return False
    if not isinstance(body_bytes, bytes) or len(body_bytes) > MAX_BODY_BYTES:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()).decode("ascii")
    try:
        return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def extract_events(payload, bot_user_id="", now=None):
    """Return normalized 1:1 user message events from a verified webhook payload.

    Each item: {"event_id", "user_id", "reply_token", "type", "text", "message_id",
    "redelivery"}. Group/room messages, standby-mode events, unsupported message
    types and events outside the replay window are dropped. An empty event list
    (LINE's console "Verify" request) yields [].
    """
    now = time.time() if now is None else now
    out = []
    if not isinstance(payload, dict):
        return out
    if bot_user_id and payload.get("destination") != bot_user_id:
        logger.warning("LINE: event for a different bot ignored")
        return out
    for event in payload.get("events") or []:
        item = _normalize_event(event, now)
        if item:
            out.append(item)
    return out


def _normalize_event(event, now):
    if not isinstance(event, dict) or event.get("type") != "message":
        return None
    if event.get("mode", "active") != "active":
        return None
    source = event.get("source") or {}
    if source.get("type") != "user":
        logger.info("LINE: non-1:1 event ignored")
        return None
    user_id = source.get("userId", "")
    event_id = event.get("webhookEventId", "")
    if not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
        return None
    if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
        return None
    redelivery = bool((event.get("deliveryContext") or {}).get("isRedelivery"))
    try:
        age = now - int(event.get("timestamp", 0)) / 1000.0
    except (TypeError, ValueError):
        return None
    limit = MAX_REDELIVERY_AGE_SECONDS if redelivery else MAX_EVENT_AGE_SECONDS
    if abs(age) > limit:
        logger.warning("LINE: event outside replay window ignored (age=%s redelivery=%s)", int(age), redelivery)
        return None
    reply_token = event.get("replyToken", "")
    if not isinstance(reply_token, str) or not _REPLY_TOKEN_RE.match(reply_token):
        reply_token = ""
    message = event.get("message") or {}
    msg_type = message.get("type")
    item = {"event_id": event_id, "user_id": user_id, "reply_token": reply_token, "type": msg_type,
            "text": "", "message_id": "", "redelivery": redelivery}
    if msg_type == "text":
        item["text"] = str(message.get("text", ""))
    elif msg_type == "image":
        message_id = str(message.get("id", ""))
        provider = (message.get("contentProvider") or {}).get("type", "line")
        if provider != "line" or not _MESSAGE_ID_RE.match(message_id):
            return None
        item["message_id"] = message_id
    else:
        return None
    return item


def split_text(text, limit=TEXT_CHUNK_CHARS):
    """Split text into chunks of at most `limit` characters, preferring line breaks."""
    text = text or ""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _post(config, url, payload, timeout, extra_headers=None):
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {config['channelAccessToken']}"}
    headers.update(extra_headers or {})
    req = urllib_request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        resp.read(1024)


def send_text(config, user_id, text, reply_token="", timeout=10):
    """Send a reply. Uses the reply token for the first batch when it is still valid,
    otherwise (or for later batches) the push API. Returns True when all chunks were sent.
    """
    if not config or not isinstance(user_id, str) or not _USER_ID_RE.match(user_id):
        logger.error("LINE send skipped: channel not configured or bad recipient")
        return False
    messages = [{"type": "text", "text": c} for c in split_text(text)]
    batches = [messages[i:i + MAX_MESSAGES_PER_REQUEST] for i in range(0, len(messages), MAX_MESSAGES_PER_REQUEST)]
    for index, batch in enumerate(batches):
        if index == 0 and reply_token:
            try:
                _post(config, f"{API_BASE}/message/reply", {"replyToken": reply_token, "messages": batch}, timeout)
                continue
            except urllib_error.HTTPError as e:
                if e.code != 400:
                    logger.error("LINE reply failed: %s", e.code)
                    return False
                # 400 = reply token invalid or expired; fall back to push.
                logger.info("LINE reply token expired; falling back to push")
            except Exception as e:
                logger.error("LINE reply failed: %s", type(e).__name__)
                return False
        try:
            # The retry key makes a retried push idempotent on LINE's side.
            _post(config, f"{API_BASE}/message/push", {"to": user_id, "messages": batch}, timeout,
                  {"X-Line-Retry-Key": str(uuid.uuid4())})
        except urllib_error.HTTPError as e:
            logger.error("LINE push failed: %s", e.code)
            return False
        except Exception as e:
            logger.error("LINE push failed: %s", type(e).__name__)
            return False
    return True


def show_loading(config, user_id, seconds=60, timeout=5):
    """Show LINE's typing animation in a 1:1 chat (free; no message quota). Best effort."""
    if not config or not _USER_ID_RE.match(str(user_id)):
        return
    try:
        _post(config, f"{API_BASE}/chat/loading/start",
              {"chatId": user_id, "loadingSeconds": max(5, min(60, int(seconds) // 5 * 5))}, timeout)
    except Exception as e:
        logger.info("LINE loading animation failed: %s", type(e).__name__)


def download_image(config, message_id, allowed_types, max_bytes, timeout=15):
    """Download an inbound image's content. Returns (bytes, content_type) or (None, None)."""
    if not config or not _MESSAGE_ID_RE.match(str(message_id)):
        return None, None
    req = urllib_request.Request(
        f"{DATA_API_BASE}/message/{message_id}/content",
        headers={"Authorization": f"Bearer {config['channelAccessToken']}"},
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            content_type = str(resp.headers.get("Content-Type", "")).split(";")[0].strip()
            if content_type not in allowed_types:
                logger.warning("LINE image rejected: unsupported type %s", content_type)
                return None, None
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.warning("LINE image rejected: over size limit")
            return None, None
        return data, content_type
    except Exception as e:
        logger.error("LINE image download failed: %s", type(e).__name__)
        return None, None
