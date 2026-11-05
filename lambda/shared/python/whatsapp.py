"""WhatsApp Business Platform (Cloud API) helpers for the Router Lambda.

Stdlib only. The channel is disabled unless the ``openclaw/channels/whatsapp``
secret is a JSON object with every key in ``REQUIRED_KEYS``; the deploy-time
placeholder (a random string) therefore leaves the endpoint closed.

Inbound:  GET  /webhook/whatsapp  -- subscription verification (hub.verify_token)
          POST /webhook/whatsapp  -- events, signed with X-Hub-Signature-256
Outbound: POST https://graph.facebook.com/<version>/<phone_number_id>/messages
"""

import hashlib
import hmac
import json
import logging
import re
import time
from urllib import request as urllib_request
from urllib.parse import quote

import formatting

logger = logging.getLogger()

REQUIRED_KEYS = ("accessToken", "appSecret", "verifyToken", "phoneNumberId")
DEFAULT_GRAPH_API_VERSION = "v23.0"
GRAPH_API_BASE = "https://graph.facebook.com"
_GRAPH_VERSION_RE = re.compile(r"^v\d{1,3}\.\d{1,2}$")
_PHONE_NUMBER_ID_RE = re.compile(r"^\d{1,32}$")
_WA_ID_RE = re.compile(r"^\d{1,32}$")
# Message types the bot cannot read yet: kept so the Router can say so. A
# document is passed on with its media ID, MIME type and file name, and the
# Router decides from those whether it can read it.
UNSUPPORTED_TYPES = frozenset({"audio", "video", "sticker", "location", "contacts"})
_CAPTIONED_TYPES = frozenset({"video"})
_MEDIA_ID_RE = re.compile(r"^\d{1,64}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# WhatsApp text bodies are limited to 4096 characters.
TEXT_CHUNK_CHARS = 4000
# Meta does not sign a timestamp, so the message's own timestamp bounds replay.
MAX_MESSAGE_AGE_SECONDS = 300
MAX_BODY_BYTES = 1_000_000


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
    for key in REQUIRED_KEYS:
        if not isinstance(data.get(key), str) or not data[key]:
            return None
    if not _PHONE_NUMBER_ID_RE.match(data["phoneNumberId"]):
        return None
    version = data.get("graphApiVersion") or DEFAULT_GRAPH_API_VERSION
    if not isinstance(version, str) or not _GRAPH_VERSION_RE.match(version):
        return None
    return {
        "accessToken": data["accessToken"],
        "appSecret": data["appSecret"],
        "verifyToken": data["verifyToken"],
        "phoneNumberId": data["phoneNumberId"],
        "graphApiVersion": version,
    }


def verify_signature(headers, body_bytes, app_secret):
    """Check X-Hub-Signature-256 = 'sha256=' + hex HMAC-SHA256(app_secret, raw body).

    Fail-closed: any missing input returns False. Constant-time compare.
    """
    if not app_secret:
        return False
    signature = (headers or {}).get("x-hub-signature-256", "")
    if not isinstance(signature, str) or not signature.startswith("sha256="):
        return False
    if not isinstance(body_bytes, bytes):
        return False
    if len(body_bytes) > MAX_BODY_BYTES:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    # Compare as bytes: hmac.compare_digest raises TypeError for a str
    # containing non-ASCII characters.
    try:
        return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def verify_subscription(query, verify_token):
    """Handle Meta's GET verification. Returns the challenge string, or None to reject."""
    if not verify_token or not isinstance(query, dict):
        return None
    if query.get("hub.mode") != "subscribe":
        return None
    token = query.get("hub.verify_token", "")
    if not isinstance(token, str) or not token:
        return None
    if not hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8")):
        return None
    challenge = query.get("hub.challenge", "")
    if not isinstance(challenge, str) or not _CHALLENGE_RE.match(challenge):
        return None
    return challenge


def extract_messages(payload, phone_number_id, now=None):
    """Yield normalized inbound user messages from a webhook payload.

    Each item: {"id", "from", "name", "type", "text", "image_id", "image_mime",
    "document_id", "document_mime", "document_name", "timestamp", "unsupported"}.
    A document keeps its caption as the text. A message of one of
    UNSUPPORTED_TYPES, or a document without a usable media ID, is kept with
    unsupported=True (and the caption of a video or document as its text), so
    the Router can say it cannot read it. Messages for another phone number,
    status updates, other types (reactions, buttons, system messages, Meta's own
    "unsupported" type) and messages older than MAX_MESSAGE_AGE_SECONDS are
    dropped.
    """
    now = time.time() if now is None else now
    out = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            if str(metadata.get("phone_number_id", "")) != phone_number_id:
                logger.warning("WhatsApp: event for a different phone number ignored")
                continue
            names = {}
            for contact in value.get("contacts") or []:
                if isinstance(contact, dict):
                    names[str(contact.get("wa_id", ""))] = str((contact.get("profile") or {}).get("name", ""))[:128]
            for msg in value.get("messages") or []:
                item = _normalize_message(msg, now)
                if item:
                    item["name"] = names.get(item["from"], "")
                    out.append(item)
    return out


def _normalize_message(msg, now):
    if not isinstance(msg, dict):
        return None
    msg_id = msg.get("id")
    sender = str(msg.get("from", ""))
    if not isinstance(msg_id, str) or not msg_id or len(msg_id) > 256:
        return None
    if not _WA_ID_RE.match(sender):
        return None
    try:
        ts = int(msg.get("timestamp", 0))
    except (TypeError, ValueError):
        return None
    if abs(now - ts) > MAX_MESSAGE_AGE_SECONDS:
        logger.warning("WhatsApp: message outside replay window ignored (age=%s)", int(now - ts))
        return None
    msg_type = msg.get("type")
    item = {"id": msg_id, "from": sender, "type": msg_type, "text": "",
            "image_id": "", "image_mime": "", "document_id": "", "document_mime": "", "document_name": "",
            "timestamp": ts, "unsupported": False}
    if msg_type == "text":
        item["text"] = str((msg.get("text") or {}).get("body", ""))
    elif msg_type == "image":
        image = msg.get("image") or {}
        media_id = str(image.get("id", ""))
        if not _MEDIA_ID_RE.match(media_id):
            return None
        item["image_id"] = media_id
        item["image_mime"] = str(image.get("mime_type", "")).split(";")[0].strip()
        item["text"] = str(image.get("caption", "") or "")
    elif msg_type == "document":
        document = msg.get("document") if isinstance(msg.get("document"), dict) else {}
        item["text"] = str(document.get("caption", "") or "")
        media_id = str(document.get("id", ""))
        if _MEDIA_ID_RE.match(media_id):
            item["document_id"] = media_id
            item["document_mime"] = str(document.get("mime_type", "")).split(";")[0].strip()[:128]
            item["document_name"] = str(document.get("filename", "") or "")[:256]
        else:
            item["unsupported"] = True
    elif msg_type in UNSUPPORTED_TYPES:
        item["unsupported"] = True
        media = msg.get(msg_type)
        if msg_type in _CAPTIONED_TYPES and isinstance(media, dict):
            item["text"] = str(media.get("caption", "") or "")
    else:
        return None
    return item


def _graph_url(config, path):
    return f"{GRAPH_API_BASE}/{config['graphApiVersion']}/{path}"


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


def send_text(config, to, text, timeout=10):
    """Send a text reply. Returns True when every chunk was accepted."""
    if not config or not _WA_ID_RE.match(str(to)):
        logger.error("WhatsApp send skipped: channel not configured or bad recipient")
        return False
    url = _graph_url(config, f"{config['phoneNumberId']}/messages")
    ok = True
    # Model Markdown in WhatsApp's own formatting (*bold*, _italic_, links as "label (url)").
    for chunk in split_text(formatting.whatsapp_text(text)):
        data = json.dumps({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": str(to),
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }).encode()
        req = urllib_request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['accessToken']}",
        })
        try:
            urllib_request.urlopen(req, timeout=timeout).close()
        except Exception as e:
            # Error type only: HTTPError text can echo request details.
            logger.error("WhatsApp send failed: %s", type(e).__name__)
            ok = False
            break
    return ok


def _fetch_media(config, media_id, allowed_types, max_bytes, timeout):
    """Download an inbound media object: (bytes, content_type, None) or (None, None, reason).

    allowed_types None accepts any type (the caller has classified the file).
    reason is "too_large" (declared by Meta, or seen while downloading) or "failed".
    """
    if not config or not _MEDIA_ID_RE.match(str(media_id)):
        return None, None, "failed"
    auth = {"Authorization": f"Bearer {config['accessToken']}"}
    try:
        meta_req = urllib_request.Request(_graph_url(config, quote(str(media_id), safe="")), headers=auth)
        with urllib_request.urlopen(meta_req, timeout=timeout) as resp:
            meta = json.loads(resp.read(65536).decode("utf-8"))
        mime = str(meta.get("mime_type", "")).split(";")[0].strip()
        size = int(meta.get("file_size", 0) or 0)
        media_url = meta.get("url", "")
        if allowed_types is not None and mime not in allowed_types:
            logger.warning("WhatsApp media rejected: unsupported type %s", mime)
            return None, None, "failed"
        if size > max_bytes:
            logger.warning("WhatsApp media rejected: %s bytes over limit", size)
            return None, None, "too_large"
        # Only follow Meta-hosted HTTPS URLs so the bearer token is never sent elsewhere.
        if not _is_meta_media_url(media_url):
            logger.warning("WhatsApp media rejected: unexpected download host")
            return None, None, "failed"
        with _NO_REDIRECT_OPENER.open(urllib_request.Request(media_url, headers=auth), timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.warning("WhatsApp media rejected: download over limit")
            return None, None, "too_large"
        return data, mime, None
    except Exception as e:
        logger.error("WhatsApp media download failed: %s", type(e).__name__)
        return None, None, "failed"


def download_media(config, media_id, allowed_types, max_bytes, timeout=15):
    """Download an inbound media object. Returns (bytes, content_type) or (None, None)."""
    data, mime, _ = _fetch_media(config, media_id, allowed_types, max_bytes, timeout)
    return data, mime


def download_document(config, media_id, max_bytes, timeout=15):
    """Download an inbound document of any type. Returns (bytes, None) or (None, reason).

    reason is "too_large" or "failed". The caller classifies the document.
    """
    data, _, reason = _fetch_media(config, media_id, None, max_bytes, timeout)
    return data, reason


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    """Refuse redirects so the bearer token is never replayed to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib_request.build_opener(_NoRedirect)


def _is_meta_media_url(url):
    from urllib.parse import urlparse
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "lookaside.fbsbx.com" or host.endswith(".fbsbx.com")
        or host == "graph.facebook.com" or host.endswith(".whatsapp.net"))
