"""Fast signed Telegram ingress that ACKs only after durable FIFO acceptance."""

from __future__ import annotations

import hmac
import json
from typing import Mapping

try:
    from .event_identity import derive_event_trace
    from .message_queue import (
        EnvelopeValidationError,
        QueueEnvelope,
        build_fifo_send_request,
    )
    from .product_commands import parse_product_command
except ImportError:
    from event_identity import derive_event_trace
    from message_queue import EnvelopeValidationError, QueueEnvelope, build_fifo_send_request
    from product_commands import parse_product_command


MAX_WEBHOOK_BYTES = 128 * 1024


class TelegramWebhookIngress:
    def __init__(
        self,
        *,
        secret_provider,
        resolve_user,
        sqs_client,
        queue_url: str,
    ) -> None:
        if not callable(secret_provider) or not callable(resolve_user):
            raise TypeError("ingress dependencies must be callable")
        self._secret_provider = secret_provider
        self._resolve_user = resolve_user
        self._sqs = sqs_client
        self._queue_url = queue_url

    def _authorized(self, headers: object) -> bool:
        if not isinstance(headers, Mapping):
            return False
        normalized = {
            str(key).casefold(): value
            for key, value in headers.items()
            if isinstance(value, str)
        }
        supplied = normalized.get("x-telegram-bot-api-secret-token", "")
        secret = self._secret_provider()
        return bool(
            isinstance(secret, str)
            and secret
            and supplied
            and hmac.compare_digest(secret, supplied)
        )

    def handle(self, body: object, headers: object) -> dict[str, object]:
        if not self._authorized(headers):
            return {"statusCode": 401, "body": "Unauthorized"}
        if not isinstance(body, str) or not body or len(body.encode("utf-8")) > MAX_WEBHOOK_BYTES:
            return {"statusCode": 400, "body": "Invalid update"}
        try:
            def reject_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            update = json.loads(
                body,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (ValueError, json.JSONDecodeError):
            return {"statusCode": 400, "body": "Invalid update"}
        if not isinstance(update, Mapping):
            return {"statusCode": 400, "body": "Invalid update"}
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int):
            return {"statusCode": 200, "body": "ok"}

        message = update.get("message")
        callback = update.get("callback_query")
        if isinstance(message, Mapping):
            text = message.get("text") or message.get("caption")
            chat = message.get("chat")
            actor = message.get("from")
            callback_data = None
        elif isinstance(callback, Mapping):
            callback_message = callback.get("message")
            text = None
            chat = (
                callback_message.get("chat")
                if isinstance(callback_message, Mapping)
                else None
            )
            actor = callback.get("from")
            callback_data = callback.get("data")
        else:
            return {"statusCode": 200, "body": "ok"}
        if (
            (callback_data is None and (not isinstance(text, str) or not text))
            or (callback_data is not None and not isinstance(callback_data, str))
            or not isinstance(chat, Mapping)
            or not isinstance(actor, Mapping)
            or chat.get("type") != "private"
            or isinstance(chat.get("id"), bool)
            or not isinstance(chat.get("id"), int)
            or isinstance(actor.get("id"), bool)
            or not isinstance(actor.get("id"), int)
            or chat.get("id") <= 0
            or actor.get("id") <= 0
            or chat.get("id") != actor.get("id")
            or len(str(chat.get("id"))) > 20
        ):
            # Media ingestion requires a separate bounded worker-side fetch
            # contract. It is deliberately a no-op until that contract exists.
            return {"statusCode": 200, "body": "ok"}
        actor_id = str(actor["id"])
        display_name = actor.get("first_name") or actor.get("username") or ""
        if not isinstance(display_name, str):
            display_name = ""
        try:
            user_id, _ = self._resolve_user("telegram", actor_id, display_name[:128])
        except Exception:
            return {"statusCode": 503, "body": "identity unavailable"}
        if user_id is None:
            return {"statusCode": 200, "body": "ok"}
        event_id = str(update_id)
        try:
            trace_id = derive_event_trace("telegram", user_id, event_id)
            command = parse_product_command(text) if callback_data is None else None
            if callback_data is not None:
                kind = "callback"
                work = {"callbackData": callback_data}
            elif command:
                kind = "command"
                work = {"command": command.name}
            else:
                kind = "message"
                work = {"message": text}
            envelope = QueueEnvelope(
                user_id=user_id,
                channel="telegram",
                update_id=event_id,
                trace_id=trace_id,
                kind=kind,
                payload={
                    "chatId": str(chat["id"]),
                    "actorId": f"telegram:{actor_id}",
                    **work,
                },
            )
            request = build_fifo_send_request(self._queue_url, envelope)
            accepted = self._sqs.send_message(**request)
            if (
                not isinstance(accepted, Mapping)
                or not accepted.get("MessageId")
                or not accepted.get("SequenceNumber")
            ):
                raise RuntimeError("SQS returned no FIFO receipt")
        except EnvelopeValidationError:
            # Invalid callback data is attacker-controlled input, not a queue
            # outage. ACK it without creating work or a Telegram retry loop.
            return {"statusCode": 200, "body": "ok"}
        except Exception:
            return {"statusCode": 503, "body": "queue unavailable"}
        return {"statusCode": 200, "body": "ok"}
