"""Single-attempt boundary from the ordered worker to the trusted control Lambda."""

from __future__ import annotations

import json
import re
from typing import Mapping

from router.product_commands import ProductCommand

try:
    from worker.telegram_cards import TelegramCommandResult
except ImportError:  # focused direct-file tests
    from telegram_cards import TelegramCommandResult


_FUNCTION = re.compile(r"[A-Za-z0-9-_]{1,64}:[A-Za-z0-9-_]{1,128}")
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_TRACE = re.compile(r"po1_[0-9a-f]{64}")
_CHAT = re.compile(r"-?[0-9]{1,20}")
_ACTOR = re.compile(r"telegram:[0-9]{1,20}")
_CALLBACK = re.compile(r"poc1:(edit|prepare|skip|why):[A-Za-z0-9_-]{22,32}")


class ControlPlaneUncertain(RuntimeError):
    pass


class LambdaProductCommandHandler:
    def __init__(self, client, *, function_name: str) -> None:
        if not isinstance(function_name, str) or _FUNCTION.fullmatch(function_name) is None:
            raise ValueError("control function name is invalid")
        self._client = client
        self._function = function_name

    def handle(
        self,
        *,
        user_id: str,
        command: ProductCommand,
        channel: str,
        trace_id: str,
        idempotency_key: str,
        chat_id: str,
        actor_id: str,
    ) -> str | TelegramCommandResult:
        if (
            not isinstance(user_id, str)
            or _USER.fullmatch(user_id) is None
            or not isinstance(command, ProductCommand)
            or channel != "telegram"
            or not isinstance(trace_id, str)
            or _TRACE.fullmatch(trace_id) is None
            or idempotency_key != trace_id
            or not isinstance(chat_id, str)
            or _CHAT.fullmatch(chat_id) is None
            or not isinstance(actor_id, str)
            or _ACTOR.fullmatch(actor_id) is None
        ):
            raise ValueError("control invocation is not event-bound")
        request = {
            "action": "productCommand",
            "userId": user_id,
            "channel": channel,
            "command": command.name,
            "chatId": chat_id,
            "actorId": actor_id,
            "traceId": trace_id,
            "idempotencyKey": idempotency_key,
        }
        return self._invoke(request, user_id=user_id, trace_id=trace_id)

    def handle_callback(
        self,
        *,
        user_id: str,
        channel: str,
        trace_id: str,
        idempotency_key: str,
        chat_id: str,
        actor_id: str,
        callback_data: str,
    ) -> str | TelegramCommandResult:
        if (
            not isinstance(user_id, str)
            or _USER.fullmatch(user_id) is None
            or channel != "telegram"
            or not isinstance(trace_id, str)
            or _TRACE.fullmatch(trace_id) is None
            or idempotency_key != trace_id
            or not isinstance(chat_id, str)
            or _CHAT.fullmatch(chat_id) is None
            or not isinstance(actor_id, str)
            or _ACTOR.fullmatch(actor_id) is None
            or not isinstance(callback_data, str)
            or _CALLBACK.fullmatch(callback_data) is None
        ):
            raise ValueError("callback invocation is not event-bound")
        request = {
            "action": "telegramCallback",
            "userId": user_id,
            "channel": channel,
            "chatId": chat_id,
            "actorId": actor_id,
            "callbackData": callback_data,
            "traceId": trace_id,
            "idempotencyKey": idempotency_key,
        }
        return self._invoke(request, user_id=user_id, trace_id=trace_id)

    def deletion_blocked(
        self,
        *,
        user_id: str,
        channel: str,
        trace_id: str,
        idempotency_key: str,
    ) -> bool:
        if (
            not isinstance(user_id, str)
            or _USER.fullmatch(user_id) is None
            or channel != "telegram"
            or not isinstance(trace_id, str)
            or _TRACE.fullmatch(trace_id) is None
            or idempotency_key != trace_id
        ):
            raise ValueError("deletion fence invocation is not event-bound")
        request = {
            "action": "deletionFence",
            "userId": user_id,
            "channel": channel,
            "traceId": trace_id,
            "idempotencyKey": idempotency_key,
        }
        result = self._invoke_raw(request)
        try:
            if (
                set(result) != {"status", "userId", "traceId", "blocked"}
                or result.get("status") != "ok"
                or result.get("userId") != user_id
                or result.get("traceId") != trace_id
                or not isinstance(result.get("blocked"), bool)
            ):
                raise ValueError("control deletion fence binding is invalid")
            return result["blocked"]
        except Exception as error:
            raise ControlPlaneUncertain(
                "trusted deletion fence is uncertain and was not retried"
            ) from error

    def _invoke_raw(self, request: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = self._client.invoke(
                FunctionName=self._function,
                InvocationType="RequestResponse",
                Payload=json.dumps(request, separators=(",", ":")).encode(),
            )
            payload_stream = response.get("Payload") if isinstance(response, Mapping) else None
            raw = payload_stream.read(64 * 1024 + 1) if hasattr(payload_stream, "read") else None
            if (
                response.get("StatusCode") != 200
                or response.get("FunctionError") is not None
                or not isinstance(raw, bytes)
                or len(raw) > 64 * 1024
            ):
                raise ValueError("control function returned no exact result")
            def reject_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            result = json.loads(
                raw,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
            if not isinstance(result, Mapping):
                raise ValueError("control function returned a non-object result")
            return result
        except Exception as error:
            raise ControlPlaneUncertain(
                "trusted control invocation is uncertain and was not retried"
            ) from error

    def _invoke(
        self,
        request: Mapping[str, object],
        *,
        user_id: str,
        trace_id: str,
    ) -> str | TelegramCommandResult:
        try:
            result = self._invoke_raw(request)
            keys = set(result) if isinstance(result, Mapping) else set()
            if (
                not isinstance(result, Mapping)
                or keys
                not in (
                    {"status", "userId", "traceId", "text"},
                    {"status", "userId", "traceId", "text", "telegram"},
                )
                or result.get("status") != "ok"
                or result.get("userId") != user_id
                or result.get("traceId") != trace_id
                or not isinstance(result.get("text"), str)
                or not 1 <= len(result["text"]) <= 3_500
            ):
                raise ValueError("control response binding is invalid")
            if "telegram" in result:
                return TelegramCommandResult.from_control(
                    text=result["text"],
                    telegram=result["telegram"],
                )
            return result["text"]
        except Exception as error:
            raise ControlPlaneUncertain(
                "trusted control invocation is uncertain and was not retried"
            ) from error
