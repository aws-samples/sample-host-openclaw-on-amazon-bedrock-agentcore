"""Router Lambda — Webhook ingestion for Telegram, Slack, and Feishu.

Receives webhook events via API Gateway HTTP API, resolves user identity via
DynamoDB, invokes the per-user AgentCore Runtime session, and sends responses
back to the originating channel.

Path routing:
  POST /webhook/telegram  — Telegram Bot API webhook
  POST /webhook/slack     — Slack Events API webhook
  POST /webhook/feishu    — Feishu Events API webhook
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
FEISHU_TOKEN_SECRET_ID = os.environ.get("FEISHU_TOKEN_SECRET_ID", "")
FEISHU_API_DOMAIN = os.environ.get("FEISHU_API_DOMAIN", "https://open.feishu.cn")
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


# Feishu tenant_access_token cache (2h TTL, refresh 5 min early)
_feishu_token_cache = {"token": "", "expires_at": 0}


def _get_feishu_tenant_token():
    """Get or refresh Feishu tenant_access_token (2h TTL, refresh 5 min early)."""
    if _feishu_token_cache["token"] and time.time() < _feishu_token_cache["expires_at"] - 300:
        return _feishu_token_cache["token"]

    app_id, app_secret, _, _ = _get_feishu_credentials()
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
        logger.error("Failed to get Feishu tenant_access_token: %s", e)
    return ""


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

    body_b = body_bytes if isinstance(body_bytes, bytes) else body_bytes.encode("utf-8")
    content = f"{timestamp}{nonce}{encrypt_key}".encode() + body_b
    expected = hashlib.sha256(content).hexdigest()

    if not hmac.compare_digest(expected, signature):
        logger.warning("Feishu webhook signature mismatch")
        return False

    return True


def _decrypt_feishu_event(encrypted_body):
    """Decrypt AES-256-CBC encrypted Feishu event body.

    Feishu encrypts events when Encrypt Key is configured.  The body arrives as
    {"encrypt": "<base64-ciphertext>"}.  The AES key is SHA-256(encrypt_key),
    the IV is the first 16 bytes of the ciphertext.

    Uses a zero-dependency AES-CBC implementation to avoid native binary
    compatibility issues across Lambda architectures.

    Returns the decrypted JSON string, or None on failure.
    """
    import base64

    _, _, _, encrypt_key = _get_feishu_credentials()
    if not encrypt_key:
        logger.error("Feishu encrypt_key not set — cannot decrypt")
        return None

    try:
        data = json.loads(encrypted_body) if isinstance(encrypted_body, str) else encrypted_body
        cipher_text = base64.b64decode(data["encrypt"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.error("Feishu decrypt: bad input — %s", e)
        return None

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = cipher_text[:16]
    encrypted = cipher_text[16:]

    try:
        decrypted = _aes_cbc_decrypt(key, iv, encrypted)
    except RuntimeError as e:
        logger.error("Feishu decrypt failed: %s", e)
        return None

    return decrypted.decode("utf-8")


def _aes_cbc_decrypt(key, iv, data):
    """AES-256-CBC decryption via system OpenSSL (libcrypto).

    Lambda runtimes always have libcrypto.so available.  This avoids shipping
    any native Python extension while still getting C-speed AES.
    """
    import ctypes
    import ctypes.util

    libcrypto_name = ctypes.util.find_library("crypto")
    if not libcrypto_name:
        # Fallback paths common on Amazon Linux 2023
        for path in ("/usr/lib64/libcrypto.so", "/usr/lib/libcrypto.so"):
            try:
                libcrypto = ctypes.CDLL(path)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("libcrypto not found")
    else:
        libcrypto = ctypes.CDLL(libcrypto_name)

    # EVP_CIPHER_CTX_new / free
    libcrypto.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    libcrypto.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]

    # EVP_DecryptInit_ex
    libcrypto.EVP_DecryptInit_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_char_p, ctypes.c_char_p,
    ]
    libcrypto.EVP_DecryptInit_ex.restype = ctypes.c_int

    # EVP_DecryptUpdate
    libcrypto.EVP_DecryptUpdate.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p, ctypes.c_int,
    ]
    libcrypto.EVP_DecryptUpdate.restype = ctypes.c_int

    # EVP_DecryptFinal_ex
    libcrypto.EVP_DecryptFinal_ex.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
    ]
    libcrypto.EVP_DecryptFinal_ex.restype = ctypes.c_int

    # EVP_aes_256_cbc
    libcrypto.EVP_aes_256_cbc.restype = ctypes.c_void_p
    libcrypto.EVP_aes_256_cbc.argtypes = []

    ctx = libcrypto.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("EVP_CIPHER_CTX_new failed")

    try:
        cipher_type = libcrypto.EVP_aes_256_cbc()
        rc = libcrypto.EVP_DecryptInit_ex(ctx, cipher_type, None, key, iv)
        if rc != 1:
            raise RuntimeError("EVP_DecryptInit_ex failed")

        out_buf = ctypes.create_string_buffer(len(data) + 16)
        out_len = ctypes.c_int(0)

        rc = libcrypto.EVP_DecryptUpdate(ctx, out_buf, ctypes.byref(out_len), data, len(data))
        if rc != 1:
            raise RuntimeError("EVP_DecryptUpdate failed")
        total = out_len.value

        final_buf = ctypes.create_string_buffer(16)
        final_len = ctypes.c_int(0)
        rc = libcrypto.EVP_DecryptFinal_ex(ctx, final_buf, ctypes.byref(final_len))
        if rc != 1:
            raise RuntimeError("EVP_DecryptFinal_ex failed — bad padding or wrong key")

        return out_buf.raw[:total] + final_buf.raw[:final_len.value]
    finally:
        libcrypto.EVP_CIPHER_CTX_free(ctx)


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
    logger.info("New session created: %s for user %s", session_id, user_id)
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
            # Not a valid content block array — check if it looks like truncated
            # content blocks (e.g., '[{"type":' ...) and strip them
            remainder = result[pos:]
            if re.match(r'^\[\{\s*"type"\s*:', remainder) or remainder.strip() == "[{":
                # Truncated content block JSON — skip the rest
                break
            rebuilt.append("[")
            i = pos + 1
        result = "".join(rebuilt)
        if result == prev:
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

    # 5. Re-insert placeholders
    for idx, content in enumerate(placeholders):
        text = text.replace(f"\x00PH{idx}\x00", content)

    return text


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


def send_feishu_message(chat_id, text):
    """Send a message via Feishu Bot API."""
    token = _get_feishu_tenant_token()
    if not token:
        logger.error("No Feishu tenant_access_token available")
        return

    url = f"{FEISHU_API_DOMAIN}/open-apis/im/v1/messages?receive_id_type=chat_id"
    MAX_FEISHU_TEXT_LEN = 20000

    chunks = [text[i:i + MAX_FEISHU_TEXT_LEN]
              for i in range(0, len(text), MAX_FEISHU_TEXT_LEN)] if len(text) > MAX_FEISHU_TEXT_LEN else [text]

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


def _feishu_progress_notify(chat_id, stop_event, notify_after_s=30):
    """Send a one-time progress message to Feishu if the request takes longer than notify_after_s."""
    if not stop_event.wait(timeout=notify_after_s):
        send_feishu_message(chat_id, "\u23f3 正在处理你的请求，可能需要几分钟。完成后会发送完整回复。")


def _download_feishu_image(content_str, msg_type):
    """Download image from Feishu API using image_key.

    Returns (image_bytes, content_type, filename) or (None, None, None).
    """
    if msg_type != "image":
        return None, None, None

    try:
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
        image_key = content.get("image_key", "")
    except (json.JSONDecodeError, TypeError):
        return None, None, None

    if not image_key:
        return None, None, None

    token = _get_feishu_tenant_token()
    if not token:
        return None, None, None

    url = f"{FEISHU_API_DOMAIN}/open-apis/im/v1/images/{image_key}"
    req = urllib_request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = urllib_request.urlopen(req, timeout=30)
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        image_bytes = resp.read(4 * 1024 * 1024)  # 4MB max
        if content_type not in ALLOWED_IMAGE_TYPES:
            logger.warning("Feishu image type %s not in allowed list", content_type)
            return None, None, None
        ext = content_type.split("/")[-1].split(";")[0]
        filename = f"feishu_{image_key}.{ext}"
        return image_bytes, content_type, filename
    except Exception as e:
        logger.error("Failed to download Feishu image %s: %s", image_key, e)
        return None, None, None



# --- Screenshot marker detection ---
SCREENSHOT_MARKER_RE = re.compile(r"\[SCREENSHOT:([^\]]+)\]")


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

    send_slack_message(channel_id, response_text, bot_token)
    return {"statusCode": 200, "body": "ok"}


def handle_feishu(body, headers=None):
    """Process a Feishu Events API webhook.

    Returns a response dict for immediate replies (url_verification).
    """
    event_data = json.loads(body) if isinstance(body, str) else body

    # Decrypt if the event body is encrypted (Encrypt Key enabled)
    if "encrypt" in event_data and "header" not in event_data:
        decrypted = _decrypt_feishu_event(event_data)
        if not decrypted:
            logger.error("Feishu: failed to decrypt event")
            return {"statusCode": 200, "body": "ok"}
        event_data = json.loads(decrypted)
        logger.info("Feishu: decrypted event successfully")

    # Feishu URL verification challenge (like Slack's url_verification)
    if event_data.get("type") == "url_verification":
        challenge = str(event_data.get("challenge", ""))
        if not re.match(r'^[a-zA-Z0-9_\-\.]{1,200}$', challenge):
            return {"statusCode": 400, "body": "Invalid challenge format"}
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"challenge": challenge}),
        }

    # Extract v2 event structure
    header = event_data.get("header", {})
    event = event_data.get("event", {})
    event_type = header.get("event_type", "")

    if event_type != "im.message.receive_v1":
        logger.info("Feishu: ignoring event type: %s", event_type)
        return {"statusCode": 200, "body": "ok"}

    # Ignore bot messages (same as Slack's bot_id check)
    sender = event.get("sender", {})
    if sender.get("sender_type") != "user":
        return {"statusCode": 200, "body": "ok"}

    sender_id = sender.get("sender_id", {}).get("open_id", "")
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")
    msg_type = message.get("message_type", "")
    content_str = message.get("content", "{}")

    # Parse message content (Feishu wraps content as JSON string)
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {}

    if msg_type == "text":
        text = content.get("text", "")
    elif msg_type == "image":
        text = ""  # Image-only — text may be empty
    else:
        text = content.get("text", str(content))

    # Group chat: strip @bot mention tags
    chat_type = message.get("chat_type", "p2p")
    if chat_type == "group":
        mentions = message.get("mentions", [])
        for mention in mentions:
            mention_key = mention.get("key", "")
            if mention_key:
                text = text.replace(mention_key, "").strip()

    has_image = msg_type == "image"
    if not sender_id or not chat_id or (not text and not has_image):
        return {"statusCode": 200, "body": "ok"}

    if len(sender_id) > 128:
        logger.warning("Feishu sender_id too long (%d chars), rejecting", len(sender_id))
        return {"statusCode": 400, "body": "Invalid sender ID"}

    # Handle bind commands BEFORE allowlist check
    actor_id = f"feishu:{sender_id}"
    is_bind, code = _is_bind_command(text)
    if is_bind:
        bound_user_id, success = redeem_bind_code(code, "feishu", sender_id)
        if success:
            send_feishu_message(chat_id, "Accounts linked successfully! Your sessions are now unified.")
        else:
            send_feishu_message(chat_id, "Invalid or expired link code. Please try again.")
        return {"statusCode": 200, "body": "ok"}

    # Resolve user identity
    resolved_user_id, is_new = resolve_user("feishu", sender_id)

    if resolved_user_id is None:
        send_feishu_message(
            chat_id,
            f"Sorry, this bot is private and requires an invitation.\n\n"
            f"Your ID: feishu:{sender_id}\n\n"
            f"Send this ID to the bot admin to request access.",
        )
        return {"statusCode": 200, "body": "ok"}

    # Handle link-accounts command
    if _is_link_command(text):
        bind_code = create_bind_code(resolved_user_id)
        send_feishu_message(
            chat_id,
            f"Your link code is: {bind_code}\n\nEnter this code on another channel within 10 minutes "
            f"by typing: link {bind_code}",
        )
        return {"statusCode": 200, "body": "ok"}

    # Build message payload (structured if image, plain string if text-only)
    agent_message = text or "hi"
    if has_image:
        namespace = actor_id.replace(":", "_")
        image_bytes, content_type, _ = _download_feishu_image(content_str, msg_type)
        if image_bytes:
            s3_key = _upload_image_to_s3(image_bytes, namespace, content_type)
            if s3_key:
                agent_message = _build_structured_message(text or "What is this image?", s3_key, content_type)
            else:
                send_feishu_message(chat_id, "Sorry, I couldn't process that image. Please try again.")
                return {"statusCode": 200, "body": "ok"}
        else:
            send_feishu_message(chat_id, "Sorry, I couldn't download that image. Please try again.")
            return {"statusCode": 200, "body": "ok"}

    # Get or create session
    session_id = get_or_create_session(resolved_user_id)

    logger.info(
        "Feishu: user=%s actor=%s session=%s msg_len=%d has_image=%s chat_type=%s",
        resolved_user_id, actor_id, session_id, len(text), has_image, chat_type,
    )

    # Invoke AgentCore with progress notification
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
    response_text = result.get("response", "Sorry, I couldn't process your message.")
    response_text = _extract_text_from_content_blocks(response_text)

    send_feishu_message(chat_id, response_text)
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
        elif channel == "feishu":
            handle_feishu(body, headers)
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

    elif path.endswith("/webhook/feishu"):
        # Feishu URL verification challenge — must respond synchronously
        try:
            event_data = json.loads(body) if isinstance(body, str) else body
            if event_data.get("type") == "url_verification":
                challenge = str(event_data.get("challenge", ""))
                if not re.match(r'^[a-zA-Z0-9_\-\.]{1,200}$', challenge):
                    return {"statusCode": 400, "body": "Invalid challenge format"}
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"challenge": challenge}),
                }
        except (json.JSONDecodeError, TypeError):
            pass

        # Validate Feishu signature
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        if not validate_feishu_webhook(headers, body_bytes):
            logger.warning("Feishu webhook validation failed from %s", http_info.get("sourceIp", "unknown"))
            return {"statusCode": 401, "body": "Unauthorized"}

        # Self-invoke async for actual processing
        _self_invoke_async("feishu", body, headers)
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
                             if k.startswith(("x-slack-", "x-lark-"))},
            }).encode(),
        )
    except Exception as e:
        logger.error("Self-invoke failed: %s", e, exc_info=True)
        # Do NOT fall back to synchronous processing — it could cause webhook
        # timeouts and the user's message will appear lost. The message is
        # already ACK'd to the platform; log the error for investigation.
