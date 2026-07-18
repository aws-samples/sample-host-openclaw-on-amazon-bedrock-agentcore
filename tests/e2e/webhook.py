"""Webhook simulation — build and POST realistic Telegram Update payloads."""

import json
import random
import re
import time
from dataclasses import dataclass
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from .config import E2EConfig


_PRIVATE_TELEGRAM_ID = re.compile(r"[1-9][0-9]{0,19}")


@dataclass
class WebhookResult:
    status_code: int
    body: str
    elapsed_ms: float


def build_telegram_payload(
    chat_id: str,
    user_id: str,
    text: str,
    *,
    display_name: str = "E2E Test User",
) -> dict:
    """Build a realistic Telegram Update JSON payload."""
    if (
        not isinstance(chat_id, str)
        or not isinstance(user_id, str)
        or _PRIVATE_TELEGRAM_ID.fullmatch(chat_id) is None
        or chat_id != user_id
    ):
        raise ValueError("E2E requires one exact private Telegram identity")
    update_id = random.randint(100_000_000, 999_999_999)
    message_id = random.randint(1, 999_999)
    ts = int(time.time())

    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {
                "id": int(user_id),
                "is_bot": False,
                "first_name": display_name,
                "language_code": "en",
            },
            "chat": {
                "id": int(chat_id),
                "first_name": display_name,
                "type": "private",
            },
            "date": ts,
            "text": text,
        },
    }


def post_webhook(cfg: E2EConfig, text: str) -> WebhookResult:
    """POST a Telegram webhook payload to the API Gateway.

    Returns WebhookResult with status code, body, and elapsed time.
    """
    payload = build_telegram_payload(
        cfg.telegram_chat_id,
        cfg.telegram_user_id,
        text,
    )
    url = f"{cfg.api_url}/webhook/telegram"
    data = json.dumps(payload).encode("utf-8")

    req = urllib_request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": cfg.webhook_secret,
        },
    )

    start = time.monotonic()
    try:
        resp = urllib_request.urlopen(req, timeout=30)
        elapsed = (time.monotonic() - start) * 1000
        body = resp.read().decode("utf-8")
        return WebhookResult(status_code=resp.status, body=body, elapsed_ms=elapsed)
    except HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        body = e.read().decode("utf-8") if e.fp else str(e)
        return WebhookResult(status_code=e.code, body=body, elapsed_ms=elapsed)
    except URLError as e:
        elapsed = (time.monotonic() - start) * 1000
        return WebhookResult(status_code=0, body=str(e.reason), elapsed_ms=elapsed)


def health_check(cfg: E2EConfig) -> WebhookResult:
    """GET /health to verify API Gateway is reachable."""
    url = f"{cfg.api_url}/health"
    req = urllib_request.Request(url)

    start = time.monotonic()
    try:
        resp = urllib_request.urlopen(req, timeout=10)
        elapsed = (time.monotonic() - start) * 1000
        body = resp.read().decode("utf-8")
        return WebhookResult(status_code=resp.status, body=body, elapsed_ms=elapsed)
    except HTTPError as e:
        elapsed = (time.monotonic() - start) * 1000
        body = e.read().decode("utf-8") if e.fp else str(e)
        return WebhookResult(status_code=e.code, body=body, elapsed_ms=elapsed)
    except URLError as e:
        elapsed = (time.monotonic() - start) * 1000
        return WebhookResult(status_code=0, body=str(e.reason), elapsed_ms=elapsed)
