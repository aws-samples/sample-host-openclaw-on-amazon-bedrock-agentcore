"""Telegram webhook payload builder and sender.

Builds realistic Telegram Update JSON payloads and POSTs them to the
API Gateway endpoint with the proper X-Telegram-Bot-Api-Secret-Token header.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

if TYPE_CHECKING:
    from tests.e2e.config import E2EConfig
    from tests.e2e.log_tailer import TailResult


@dataclass(frozen=True)
class WebhookResult:
    """Result of a single webhook POST."""

    status_code: int
    body: str
    elapsed_ms: float
    update_id: int


def build_telegram_update(chat_id: str, user_id: str, text: str) -> dict:
    """Build a realistic Telegram Update JSON payload.

    Uses randomized update_id/message_id and current timestamp to
    simulate real webhook traffic.
    """
    update_id = random.randint(100_000_000, 999_999_999)
    message_id = random.randint(1, 999_999)

    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {
                "id": int(user_id),
                "is_bot": False,
                "first_name": "E2E_Test",
                "username": "e2e_test_user",
                "language_code": "en",
            },
            "chat": {
                "id": int(chat_id),
                "first_name": "E2E_Test",
                "username": "e2e_test_user",
                "type": "private",
            },
            "date": int(time.time()),
            "text": text,
        },
    }


def send_webhook(
    config: E2EConfig,
    text: str,
    *,
    include_secret: bool = True,
    wrong_secret: bool = False,
) -> WebhookResult:
    """POST a Telegram webhook payload to the API Gateway.

    Args:
        config: E2E configuration.
        text: Message text to send.
        include_secret: Whether to include the secret header.
        wrong_secret: If True, send an incorrect secret value.

    Returns:
        WebhookResult with status code, body, timing, and update_id.
    """
    payload = build_telegram_update(config.chat_id, config.user_id, text)
    data = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if include_secret:
        secret = "wrong-secret-value" if wrong_secret else config.webhook_secret
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret

    req = urllib_request.Request(
        config.webhook_url,
        data=data,
        headers=headers,
        method="POST",
    )

    start = time.monotonic()
    try:
        resp = urllib_request.urlopen(req, timeout=30)
        elapsed_ms = (time.monotonic() - start) * 1000
        body = resp.read().decode("utf-8")
        return WebhookResult(
            status_code=resp.status,
            body=body,
            elapsed_ms=elapsed_ms,
            update_id=payload["update_id"],
        )
    except HTTPError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        body = e.read().decode("utf-8") if e.fp else str(e)
        return WebhookResult(
            status_code=e.code,
            body=body,
            elapsed_ms=elapsed_ms,
            update_id=payload["update_id"],
        )
    except URLError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return WebhookResult(
            status_code=0,
            body=str(e.reason),
            elapsed_ms=elapsed_ms,
            update_id=payload["update_id"],
        )


def send_conversation(
    config: E2EConfig,
    messages: list[str],
    *,
    delay_between: float = 5.0,
    tail_fn=None,
    tail_timeout: int = 300,
) -> list[tuple[WebhookResult, TailResult | None]]:
    """Send a sequence of messages with delays, optionally tailing logs after each.

    Simulates natural turn-taking: sends a message, waits for the bot to
    respond (verified via log tailing), then sends the next message.

    Args:
        config: E2E configuration.
        messages: List of message texts to send in order.
        delay_between: Seconds to wait between messages (after log verification).
        tail_fn: Optional callable(config, start_time, chat_id, timeout) -> TailResult.
                 If provided, tails logs after each message to verify response.
        tail_timeout: Timeout in seconds for each log tail.

    Returns:
        List of (WebhookResult, TailResult | None) tuples, one per message.
    """
    results = []

    for i, text in enumerate(messages):
        start_time = int(time.time() * 1000)
        webhook_result = send_webhook(config, text)

        tail_result = None
        if tail_fn is not None and webhook_result.status_code == 200:
            tail_result = tail_fn(
                config,
                start_time=start_time,
                chat_id=config.chat_id,
                timeout=tail_timeout,
            )

        results.append((webhook_result, tail_result))

        # Wait between messages (skip after last message)
        if i < len(messages) - 1:
            time.sleep(delay_between)

    return results
