"""Single-attempt Telegram delivery behind the trusted outbox fence."""

from __future__ import annotations

import html as html_escape
import json
import re
from urllib import request as urllib_request

try:
    from worker.telegram_cards import (
        TelegramCardValidationError,
        validate_reply_markup,
    )
except ImportError:  # focused direct-file tests
    from telegram_cards import TelegramCardValidationError, validate_reply_markup


MAX_RESPONSE_BYTES = 64 * 1024
TELEGRAM_MAX_HTML_CHARS = 4_096
_CHAT_ID = re.compile(r"-?[0-9]{1,20}")
_TRACE_ID = re.compile(r"po1_[0-9a-f]{64}")
_TOKEN = re.compile(r"[0-9]{3,20}:[A-Za-z0-9_-]{20,256}")
_CALLBACK_QUERY_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


class TelegramDeliveryValidationError(ValueError):
    pass


class TelegramDeliveryUncertain(RuntimeError):
    pass


def validate_safe_telegram_html(html: str) -> str:
    """Return already-rendered HTML only when one provider attempt is safe."""

    if not isinstance(html, str) or not html:
        raise TelegramDeliveryValidationError("Telegram HTML must fit one message")
    try:
        units = len(html.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise TelegramDeliveryValidationError(
            "Telegram HTML contains invalid Unicode"
        ) from None
    if units > TELEGRAM_MAX_HTML_CHARS:
        raise TelegramDeliveryValidationError("Telegram HTML must fit one message")
    return html


def render_safe_telegram_html(text: str) -> str:
    """Escape untrusted text, allow minimal markup, then enforce the shared bound."""

    if not isinstance(text, str) or not text:
        raise TelegramDeliveryValidationError("Telegram source text is invalid")
    rendered = html_escape.escape(text, quote=False)
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", rendered)
    return validate_safe_telegram_html(rendered)


class TelegramDeliveryAdapter:
    def __init__(
        self,
        *,
        token_provider,
        opener=None,
        timeout_seconds: int = 15,
    ) -> None:
        if not callable(token_provider):
            raise TypeError("token_provider must be callable")
        if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 30:
            raise ValueError("Telegram timeout must be between 1 and 30 seconds")
        self._token_provider = token_provider
        self._opener = opener or urllib_request.urlopen
        self._timeout = timeout_seconds

    def acknowledge_callback(self, *, callback_query_id: str) -> None:
        """Best-effort UI acknowledgement, separate from business delivery."""

        if (
            not isinstance(callback_query_id, str)
            or _CALLBACK_QUERY_ID.fullmatch(callback_query_id) is None
        ):
            raise TelegramDeliveryValidationError(
                "invalid Telegram callback query identity"
            )
        token = self._token_provider()
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise TelegramDeliveryValidationError("Telegram bot token is unavailable")
        payload = json.dumps(
            {
                "callback_query_id": callback_query_id,
                "text": "Working...",
                "show_alert": False,
                "cache_time": 0,
            },
            separators=(",", ":"),
        ).encode()
        request = urllib_request.Request(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Telegram response exceeded its bound")
            decoded = json.loads(raw)
            if (
                not isinstance(decoded, dict)
                or decoded.get("ok") is not True
                or decoded.get("result") is not True
            ):
                raise ValueError("Telegram returned no callback acknowledgement")
        except Exception as error:
            raise TelegramDeliveryUncertain(
                "Telegram callback acknowledgement outcome is uncertain"
            ) from error

    def send_message(
        self,
        *,
        chat_id: str,
        html: str,
        reply_markup=None,
        trace_id: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if not isinstance(chat_id, str) or _CHAT_ID.fullmatch(chat_id) is None:
            raise TelegramDeliveryValidationError("invalid Telegram chat identity")
        validate_safe_telegram_html(html)
        try:
            checked_markup = validate_reply_markup(reply_markup)
        except TelegramCardValidationError as error:
            raise TelegramDeliveryValidationError(str(error)) from error
        if (
            not isinstance(trace_id, str)
            or _TRACE_ID.fullmatch(trace_id) is None
            or idempotency_key != trace_id
        ):
            raise TelegramDeliveryValidationError("delivery identity is not event-bound")
        token = self._token_provider()
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise TelegramDeliveryValidationError("Telegram bot token is unavailable")
        provider_payload = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if checked_markup is not None:
            provider_payload["reply_markup"] = checked_markup
        payload = json.dumps(
            provider_payload,
            separators=(",", ":"),
        ).encode()
        request = urllib_request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Telegram response exceeded its bound")
            decoded = json.loads(raw)
            result = decoded.get("result") if isinstance(decoded, dict) else None
            provider_id = result.get("message_id") if isinstance(result, dict) else None
            if decoded.get("ok") is not True or isinstance(provider_id, bool) or not isinstance(
                provider_id, (int, str)
            ):
                raise ValueError("Telegram returned no exact message receipt")
            provider_id = str(provider_id)
            if not provider_id or len(provider_id) > 256:
                raise ValueError("Telegram message receipt is invalid")
            return {"providerMessageId": provider_id}
        except Exception as error:
            raise TelegramDeliveryUncertain(
                "Telegram delivery outcome is uncertain and was not retried"
            ) from error
