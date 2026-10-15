"""Discord Interactions (slash command) helpers for the Router Lambda.

Discord delivers ordinary messages only over its Gateway websocket, which a
Lambda cannot hold open. This channel uses the HTTP Interactions endpoint
instead: users talk to the bot with the /ask slash command (in DMs and in
servers). Stdlib only; Ed25519 verification uses the system OpenSSL
(libcrypto), as the Feishu AES decrypt in the router already does.

The channel is disabled unless the ``openclaw/channels/discord`` secret is a
JSON object with ``applicationId`` and ``publicKey``; the deploy-time
placeholder (a random string) leaves the endpoint closed.
"""

import ctypes
import ctypes.util
import json
import logging
import re
import threading
import time
from urllib import request as urllib_request
from urllib.parse import urlparse

logger = logging.getLogger()

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/aws-samples/sample-host-openclaw-on-amazon-bedrock-agentcore, 1.0)"
_SNOWFLAKE_RE = re.compile(r"^\d{1,25}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX128_RE = re.compile(r"^[0-9a-fA-F]{128}$")
_TIMESTAMP_RE = re.compile(r"^\d{1,12}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{1,1000}$")

MAX_TIMESTAMP_SKEW_SECONDS = 300
MAX_BODY_BYTES = 1_000_000
# Discord message content is limited to 2000 characters.
MESSAGE_CHUNK_CHARS = 1900
EPHEMERAL = 1 << 6

INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
RESPONSE_PONG = 1
RESPONSE_CHANNEL_MESSAGE = 4
RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5

_EVP_PKEY_ED25519 = 1087  # NID_ED25519


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
    app_id = data.get("applicationId")
    public_key = data.get("publicKey")
    if not isinstance(app_id, str) or not _SNOWFLAKE_RE.match(app_id):
        return None
    if not isinstance(public_key, str) or not _HEX64_RE.match(public_key):
        return None
    return {"applicationId": app_id, "publicKey": public_key.lower()}


# ---------------------------------------------------------------------------
# Ed25519 via system OpenSSL
# ---------------------------------------------------------------------------

_lib = None
_lib_lock = threading.Lock()


def _libcrypto():
    global _lib
    with _lib_lock:
        if _lib is not None:
            return _lib
        name = ctypes.util.find_library("crypto")
        candidates = [name] if name else []
        candidates += ["libcrypto.so.3", "/usr/lib64/libcrypto.so.3", "/usr/lib64/libcrypto.so"]
        lib = None
        for candidate in candidates:
            try:
                lib = ctypes.CDLL(candidate)
                break
            except OSError:
                continue
        if lib is None:
            raise RuntimeError("libcrypto not found")
        lib.EVP_PKEY_new_raw_public_key.restype = ctypes.c_void_p
        lib.EVP_PKEY_new_raw_public_key.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        lib.EVP_PKEY_free.argtypes = [ctypes.c_void_p]
        lib.EVP_MD_CTX_new.restype = ctypes.c_void_p
        lib.EVP_MD_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_DigestVerifyInit.restype = ctypes.c_int
        lib.EVP_DigestVerifyInit.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p]
        lib.EVP_DigestVerify.restype = ctypes.c_int
        lib.EVP_DigestVerify.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                         ctypes.c_char_p, ctypes.c_size_t]
        _lib = lib
        return lib


def ed25519_verify(public_key, signature, message):
    """Return True only if OpenSSL reports a valid Ed25519 signature. Never raises."""
    if not (isinstance(public_key, bytes) and len(public_key) == 32
            and isinstance(signature, bytes) and len(signature) == 64
            and isinstance(message, bytes)):
        return False
    try:
        lib = _libcrypto()
    except (RuntimeError, AttributeError) as e:
        logger.error("Ed25519 unavailable: %s", type(e).__name__)
        return False
    pkey = lib.EVP_PKEY_new_raw_public_key(_EVP_PKEY_ED25519, None, public_key, 32)
    if not pkey:
        return False
    ctx = lib.EVP_MD_CTX_new()
    try:
        if not ctx or lib.EVP_DigestVerifyInit(ctx, None, None, None, pkey) != 1:
            return False
        return lib.EVP_DigestVerify(ctx, signature, 64, message, len(message)) == 1
    finally:
        if ctx:
            lib.EVP_MD_CTX_free(ctx)
        lib.EVP_PKEY_free(pkey)


def verify_signature(headers, body_bytes, public_key_hex, now=None):
    """Check X-Signature-Ed25519 over X-Signature-Timestamp + raw body. Fail-closed."""
    headers = headers or {}
    signature = headers.get("x-signature-ed25519", "")
    timestamp = headers.get("x-signature-timestamp", "")
    if not public_key_hex or not _HEX64_RE.match(public_key_hex):
        return False
    if not isinstance(signature, str) or not _HEX128_RE.match(signature):
        return False
    if not isinstance(timestamp, str) or not _TIMESTAMP_RE.match(timestamp):
        return False
    if not isinstance(body_bytes, bytes) or len(body_bytes) > MAX_BODY_BYTES:
        return False
    now = time.time() if now is None else now
    if abs(now - int(timestamp)) > MAX_TIMESTAMP_SKEW_SECONDS:
        logger.warning("Discord interaction timestamp outside replay window")
        return False
    return ed25519_verify(bytes.fromhex(public_key_hex), bytes.fromhex(signature),
                          timestamp.encode("ascii") + body_bytes)


# ---------------------------------------------------------------------------
# Interaction parsing
# ---------------------------------------------------------------------------

def parse_command(interaction, application_id):
    """Normalize an APPLICATION_COMMAND interaction, or return None if unusable.

    Returns {"id", "token", "user_id", "name", "command", "text", "attachment",
    "in_guild"}. `text` is what the router treats as the user's message.
    """
    if not isinstance(interaction, dict) or interaction.get("type") != INTERACTION_APPLICATION_COMMAND:
        return None
    if str(interaction.get("application_id", "")) != application_id:
        logger.warning("Discord interaction for a different application ignored")
        return None
    token = interaction.get("token")
    interaction_id = str(interaction.get("id", ""))
    if not isinstance(token, str) or not _TOKEN_RE.match(token) or not _SNOWFLAKE_RE.match(interaction_id):
        return None
    member = interaction.get("member") or {}
    user = member.get("user") or interaction.get("user") or {}
    user_id = str(user.get("id", ""))
    if not _SNOWFLAKE_RE.match(user_id) or user.get("bot"):
        return None
    data = interaction.get("data") or {}
    command = data.get("name")
    options = {o.get("name"): o.get("value") for o in (data.get("options") or []) if isinstance(o, dict)}
    attachment = None
    if command == "ask":
        text = str(options.get("prompt") or "").strip()
        attachment_id = options.get("image")
        if attachment_id is not None:
            attachments = ((data.get("resolved") or {}).get("attachments") or {})
            attachment = attachments.get(str(attachment_id))
    elif command == "link":
        code = str(options.get("code") or "").strip()
        text = f"link {code}" if code else "link accounts"
    else:
        return None
    name = str(user.get("global_name") or user.get("username") or "")[:128]
    return {"id": interaction_id, "token": token, "user_id": user_id, "name": name,
            "command": command, "text": text, "attachment": attachment,
            "in_guild": bool(interaction.get("guild_id"))}


# ---------------------------------------------------------------------------
# Outbound: interaction webhooks (no bot token needed)
# ---------------------------------------------------------------------------

def split_text(text, limit=MESSAGE_CHUNK_CHARS):
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
    return chunks or [""]


def _webhook_request(method, path, payload, timeout):
    req = urllib_request.Request(
        f"{API_BASE}{path}", data=json.dumps(payload).encode(), method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        resp.read(1024)


def send_reply(application_id, token, text, ephemeral=False, timeout=10):
    """Replace the deferred 'thinking' message with the reply; extra chunks go as follow-ups.

    Returns True when every chunk was accepted.
    """
    if not _SNOWFLAKE_RE.match(str(application_id)) or not isinstance(token, str) or not _TOKEN_RE.match(token):
        logger.error("Discord reply skipped: bad application ID or token")
        return False
    base = f"/webhooks/{application_id}/{token}"
    allowed_mentions = {"parse": []}  # never ping @everyone/roles/users from model output
    try:
        chunks = split_text(text)
        _webhook_request("PATCH", f"{base}/messages/@original",
                         {"content": chunks[0], "allowed_mentions": allowed_mentions}, timeout)
        for chunk in chunks[1:]:
            payload = {"content": chunk, "allowed_mentions": allowed_mentions}
            if ephemeral:
                payload["flags"] = EPHEMERAL
            _webhook_request("POST", base, payload, timeout)
        return True
    except Exception as e:
        # Error type only: the URL contains the interaction token.
        logger.error("Discord reply failed: %s", type(e).__name__)
        return False


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib_request.build_opener(_NoRedirect)
_CDN_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


def download_attachment(attachment, allowed_types, max_bytes, timeout=15):
    """Download a slash-command attachment from Discord's CDN. Returns (bytes, type) or (None, None)."""
    if not isinstance(attachment, dict):
        return None, None
    content_type = str(attachment.get("content_type", "")).split(";")[0].strip()
    url = attachment.get("url", "")
    try:
        size = int(attachment.get("size", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    if content_type not in allowed_types:
        logger.warning("Discord attachment rejected: unsupported type %s", content_type)
        return None, None
    if size > max_bytes:
        logger.warning("Discord attachment rejected: %s bytes over limit", size)
        return None, None
    parsed = urlparse(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or (parsed.hostname or "").lower() not in _CDN_HOSTS:
        logger.warning("Discord attachment rejected: unexpected host")
        return None, None
    try:
        req = urllib_request.Request(url, headers={"User-Agent": USER_AGENT})
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.warning("Discord attachment rejected: download over limit")
            return None, None
        return data, content_type
    except Exception as e:
        logger.error("Discord attachment download failed: %s", type(e).__name__)
        return None, None
