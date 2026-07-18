"""Trusted ordered Telegram worker.

Provider credentials belong to the injected delivery adapter. This module
passes only a minimal allowlisted request to ``RuntimeDriver`` and implements
SQS FIFO partial-batch failure semantics without importing an AWS SDK.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from message_queue import QueueEnvelope
from product_commands import ProductCommand, parse_product_command


logger = logging.getLogger(__name__)

MAX_RUNTIME_RESPONSE_CHARS = 500_000
MAX_SQS_BATCH_SIZE = 10


class WorkerContractError(RuntimeError):
    """A dependency returned data that cannot cross the trusted boundary."""


class RuntimeDriver(Protocol):
    def invoke(
        self,
        user_id: str,
        request: Mapping[str, Any],
        trace_id: str,
    ) -> Mapping[str, Any]: ...


class ProductCommandHandler(Protocol):
    def handle(
        self,
        *,
        user_id: str,
        command: ProductCommand,
        channel: str,
        trace_id: str,
        idempotency_key: str,
    ) -> str: ...


class TelegramDelivery(Protocol):
    def send_message(
        self,
        *,
        chat_id: str,
        html: str,
        trace_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...


class ProcessingLedger(Protocol):
    """Persistent result/outbox ledger implemented by the trusted control plane."""

    def get_result(self, key: str) -> str | None: ...

    def put_result_if_absent(self, key: str, result: str) -> str: ...

    def is_delivered(self, key: str) -> bool: ...

    def mark_delivered(self, key: str, receipt: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    runtime_driver: RuntimeDriver
    command_handler: ProductCommandHandler
    telegram_delivery: TelegramDelivery
    ledger: ProcessingLedger


def render_telegram_html(text: str) -> str:
    """Escape runtime text, then enable only a small Telegram-safe subset."""

    if not isinstance(text, str) or not text or len(text) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("runtime response must be non-empty bounded text")
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    return escaped


def _extract_runtime_text(result: Any) -> str:
    if not isinstance(result, Mapping):
        raise WorkerContractError("runtime returned a non-object response")
    if result.get("streamed"):
        raise WorkerContractError("runtime may not stream directly to Telegram")
    response = result.get("response")
    if not isinstance(response, str) or not response or len(response) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("runtime response must contain bounded text")

    # The bridge normally returns plain text. Retain compatibility with its old
    # JSON content-block wrapper without trusting arbitrary block types.
    candidate = response.strip()
    if candidate.startswith("["):
        try:
            blocks = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            blocks = None
        if isinstance(blocks, list) and blocks and all(
            isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            for block in blocks
        ):
            response = "".join(block["text"] for block in blocks)
    if not response or len(response) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("extracted runtime response is invalid")
    return response


def _command_result(
    envelope: QueueEnvelope,
    idempotency_key: str,
    dependencies: WorkerDependencies,
) -> str:
    command = parse_product_command(envelope.payload["command"])
    if command is None:
        raise WorkerContractError("worker received an unknown product command")
    result = dependencies.command_handler.handle(
        user_id=envelope.user_id,
        command=command,
        channel=envelope.channel,
        trace_id=envelope.trace_id,
        idempotency_key=idempotency_key,
    )
    if not isinstance(result, str) or not result or len(result) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("command handler returned invalid text")
    return result


def _runtime_result(
    envelope: QueueEnvelope,
    idempotency_key: str,
    dependencies: WorkerDependencies,
) -> str:
    payload = envelope.payload
    # Never forward chatId or arbitrary envelope fields. Provider secrets cannot
    # enter this allowlisted object because the envelope rejected those keys.
    request = {
        "channel": envelope.channel,
        "actorId": payload["actorId"],
        "message": payload["message"],
        "invocationId": idempotency_key,
    }
    result = dependencies.runtime_driver.invoke(envelope.user_id, request, envelope.trace_id)
    return _extract_runtime_text(result)


def process_envelope(envelope: QueueEnvelope, dependencies: WorkerDependencies) -> None:
    """Process one validated update with durable result and delivery dedupe."""

    if not isinstance(envelope, QueueEnvelope):
        raise TypeError("envelope must be a QueueEnvelope")
    if not isinstance(dependencies, WorkerDependencies):
        raise TypeError("dependencies must be WorkerDependencies")

    key = envelope.message_deduplication_id
    result = dependencies.ledger.get_result(key)
    if result is None:
        if envelope.kind == "command":
            proposed_result = _command_result(envelope, key, dependencies)
        elif envelope.kind == "message":
            proposed_result = _runtime_result(envelope, key, dependencies)
        else:  # QueueEnvelope already excludes this; retain fail-closed defense.
            raise WorkerContractError("unsupported worker message kind")
        result = dependencies.ledger.put_result_if_absent(key, proposed_result)

    if not isinstance(result, str) or not result or len(result) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("ledger returned invalid result text")
    if dependencies.ledger.is_delivered(key):
        return

    receipt = dependencies.telegram_delivery.send_message(
        chat_id=envelope.payload["chatId"],
        html=render_telegram_html(result),
        trace_id=envelope.trace_id,
        idempotency_key=key,
    )
    if not isinstance(receipt, Mapping) or not receipt.get("providerMessageId"):
        raise WorkerContractError("Telegram delivery returned no provider receipt")
    dependencies.ledger.mark_delivered(key, receipt)


def _record_identifier(record: Any, fallback_index: int) -> str:
    if isinstance(record, Mapping):
        message_id = record.get("messageId")
        if isinstance(message_id, str) and 1 <= len(message_id) <= 128:
            return message_id
    return f"invalid-record-{fallback_index}"


def _envelope_from_sqs_record(record: Any) -> QueueEnvelope:
    if not isinstance(record, Mapping) or record.get("eventSource") != "aws:sqs":
        raise WorkerContractError("worker accepts only SQS event records")
    envelope = QueueEnvelope.from_json(record.get("body"))
    attributes = record.get("attributes")
    if not isinstance(attributes, Mapping):
        raise WorkerContractError("SQS FIFO attributes are missing")
    if attributes.get("MessageGroupId") != envelope.message_group_id:
        raise WorkerContractError("SQS message group does not match the envelope user")
    if attributes.get("MessageDeduplicationId") != envelope.message_deduplication_id:
        raise WorkerContractError("SQS deduplication ID does not match the envelope update")
    return envelope


def process_sqs_event(
    event: Any,
    dependencies: WorkerDependencies,
) -> dict[str, list[dict[str, str]]]:
    """Return Lambda's ReportBatchItemFailures response for one FIFO batch.

    Processing stops at the first failed record and marks all later records for
    retry, preserving FIFO order even when Lambda supplied multiple records.
    CDK configures the retry count and dead-letter queue; this function does not
    implement a second retry loop.
    """

    if not isinstance(event, Mapping) or not isinstance(event.get("Records"), list):
        raise WorkerContractError("worker event must contain SQS Records")
    records = event["Records"]
    if len(records) > MAX_SQS_BATCH_SIZE:
        raise WorkerContractError("SQS batch exceeds configured maximum")

    failures: list[dict[str, str]] = []
    for index, record in enumerate(records):
        try:
            process_envelope(_envelope_from_sqs_record(record), dependencies)
        except Exception as error:  # AWS needs a partial-batch response for all retryable failures.
            logger.warning(
                "Telegram FIFO record failed: message_id=%s error_type=%s",
                _record_identifier(record, index),
                type(error).__name__,
            )
            failures.extend(
                {"itemIdentifier": _record_identifier(item, later_index)}
                for later_index, item in enumerate(records[index:], start=index)
            )
            break
    return {"batchItemFailures": failures}


_dependency_factory: Callable[[], WorkerDependencies] | None = None


def configure_dependency_factory(factory: Callable[[], WorkerDependencies]) -> None:
    """Install the deployment composition root without creating clients here."""

    global _dependency_factory
    if not callable(factory):
        raise TypeError("dependency factory must be callable")
    _dependency_factory = factory


def lambda_handler(event: Any, context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    if _dependency_factory is None:
        raise RuntimeError("worker dependencies are not configured")
    return process_sqs_event(event, _dependency_factory())
