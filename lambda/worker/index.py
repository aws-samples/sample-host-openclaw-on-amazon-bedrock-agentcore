"""Trusted ordered Telegram worker.

Provider credentials belong to the injected delivery adapter. This module
passes only a minimal allowlisted request to ``RuntimeDriver`` and implements
SQS FIFO partial-batch failure semantics without importing an AWS SDK.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

try:
    from router.message_queue import QueueEnvelope
    from router.product_commands import ProductCommand, parse_product_command
except ImportError:  # focused tests add lambda/router directly to sys.path
    from message_queue import QueueEnvelope
    from product_commands import ProductCommand, parse_product_command


logger = logging.getLogger(__name__)

MAX_RUNTIME_RESPONSE_CHARS = 3_500
MAX_TELEGRAM_HTML_CHARS = 4_096
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
    """Durable claim/result/outbox state machine for one immutable event."""

    def claim_processing(self, envelope: QueueEnvelope, *, owner: str): ...

    def complete_result(self, claim, result: str): ...

    def mark_processing_uncertain(self, claim, *, error_type: str) -> None: ...

    def begin_delivery(self, claim, *, owner: str): ...

    def confirm_delivery(self, delivery_claim, receipt: Mapping[str, Any]) -> None: ...

    def mark_delivery_uncertain(self, delivery_claim, *, error_type: str) -> None: ...


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
    if len(escaped) > MAX_TELEGRAM_HTML_CHARS:
        raise WorkerContractError("rendered Telegram message exceeds one safe send")
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
    }
    result = dependencies.runtime_driver.invoke(envelope.user_id, request, envelope.trace_id)
    return _extract_runtime_text(result)


def process_envelope(envelope: QueueEnvelope, dependencies: WorkerDependencies) -> None:
    """Process one update with durable execution and at-most-one delivery attempt."""

    if not isinstance(envelope, QueueEnvelope):
        raise TypeError("envelope must be a QueueEnvelope")
    if not isinstance(dependencies, WorkerDependencies):
        raise TypeError("dependencies must be WorkerDependencies")

    owner = f"worker-{uuid.uuid4().hex}"
    claim = dependencies.ledger.claim_processing(envelope, owner=owner)
    state = getattr(claim, "state", None)
    if state == "DELIVERED":
        return
    if state in {
        "PROCESSING",
        "PROCESSING_UNCERTAIN",
        "DELIVERY_IN_FLIGHT",
        "DELIVERY_UNCERTAIN",
    }:
        raise WorkerContractError(
            f"event is {state.lower()} and will not be executed or sent again"
        )

    if state == "CLAIMED":
        try:
            if envelope.kind == "command":
                proposed_result = _command_result(
                    envelope, envelope.message_deduplication_id, dependencies
                )
            elif envelope.kind == "message":
                proposed_result = _runtime_result(
                    envelope, envelope.message_deduplication_id, dependencies
                )
            else:
                raise WorkerContractError("unsupported worker message kind")
            claim = dependencies.ledger.complete_result(claim, proposed_result)
        except Exception as error:
            try:
                dependencies.ledger.mark_processing_uncertain(
                    claim, error_type=type(error).__name__
                )
            except Exception:
                pass
            raise
        state = getattr(claim, "state", None)

    if state != "RESULT_READY":
        raise WorkerContractError("ledger returned an invalid processing state")
    result = getattr(claim, "result", None)
    if not isinstance(result, str) or not result or len(result) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("ledger returned invalid result text")

    delivery_claim = dependencies.ledger.begin_delivery(claim, owner=owner)
    delivery_state = getattr(delivery_claim, "state", None)
    if delivery_state == "DELIVERED":
        return
    if delivery_state != "DELIVERY_CLAIMED":
        raise WorkerContractError(
            "delivery is already in flight or uncertain and will not be sent again"
        )

    try:
        receipt = dependencies.telegram_delivery.send_message(
            chat_id=envelope.payload["chatId"],
            html=render_telegram_html(result),
            trace_id=envelope.trace_id,
            idempotency_key=envelope.message_deduplication_id,
        )
        if not isinstance(receipt, Mapping) or not receipt.get("providerMessageId"):
            raise WorkerContractError("Telegram delivery returned no provider receipt")
        dependencies.ledger.confirm_delivery(delivery_claim, receipt)
    except Exception as error:
        try:
            dependencies.ledger.mark_delivery_uncertain(
                delivery_claim, error_type=type(error).__name__
            )
        except Exception:
            pass
        raise


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
_production_dependencies: WorkerDependencies | None = None


def configure_dependency_factory(factory: Callable[[], WorkerDependencies]) -> None:
    """Install the deployment composition root without creating clients here."""

    global _dependency_factory
    if not callable(factory):
        raise TypeError("dependency factory must be callable")
    _dependency_factory = factory


def _build_production_dependencies() -> WorkerDependencies:
    """Create exact-region trusted clients only inside the deployed worker."""

    global _production_dependencies
    if _production_dependencies is not None:
        return _production_dependencies
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region != "eu-west-1":
        raise RuntimeError("worker requires exact eu-west-1 region")
    required = {
        name: os.environ.get(name, "")
        for name in (
            "AGENTCORE_RUNTIME_ARN",
            "AGENTCORE_QUALIFIER",
            "RUNTIME_STATE_TABLE_NAME",
            "MESSAGE_LEDGER_TABLE_NAME",
            "TELEGRAM_TOKEN_SECRET_ID",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"worker configuration missing: {','.join(sorted(missing))}")

    import boto3
    from botocore.config import Config
    try:
        from router.product_commands import DeterministicProductCommandHandler
        from router.runtime_driver import AgentCoreAdapter, RuntimeDriver
        from router.runtime_state import RuntimeStateRepository
        from worker.processing_ledger import DynamoProcessingLedger
        from worker.telegram_delivery import TelegramDeliveryAdapter
    except ImportError:  # direct lambda/worker asset is unsupported but testable
        from product_commands import DeterministicProductCommandHandler
        from runtime_driver import AgentCoreAdapter, RuntimeDriver
        from runtime_state import RuntimeStateRepository
        from processing_ledger import DynamoProcessingLedger
        from telegram_delivery import TelegramDeliveryAdapter

    no_retries = Config(retries={"max_attempts": 0})
    dynamodb = boto3.resource("dynamodb", region_name=region, config=no_retries)
    agentcore = boto3.client(
        "bedrock-agentcore",
        region_name=region,
        config=Config(
            connect_timeout=10,
            read_timeout=285,
            retries={"max_attempts": 0},
        ),
    )
    secrets = boto3.client("secretsmanager", region_name=region, config=no_retries)
    token_cache: list[str] = []

    def telegram_token() -> str:
        if token_cache:
            return token_cache[0]
        response = secrets.get_secret_value(
            SecretId=required["TELEGRAM_TOKEN_SECRET_ID"]
        )
        raw = response.get("SecretString")
        if not isinstance(raw, str) or not raw:
            raise RuntimeError("Telegram token secret is unavailable")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        token = (
            parsed.get("bot_token") or parsed.get("token")
            if isinstance(parsed, Mapping)
            else raw
        )
        if not isinstance(token, str) or not token:
            raise RuntimeError("Telegram token secret has no token")
        token_cache.append(token)
        return token

    lease_ms = int(os.environ.get("RUNTIME_LEASE_MS", "600000"))
    _production_dependencies = WorkerDependencies(
        runtime_driver=RuntimeDriver(
            repository=RuntimeStateRepository(
                dynamodb.Table(required["RUNTIME_STATE_TABLE_NAME"])
            ),
            adapter=AgentCoreAdapter(
                agentcore,
                runtime_arn=required["AGENTCORE_RUNTIME_ARN"],
                qualifier=required["AGENTCORE_QUALIFIER"],
                region=region,
            ),
            lease_ms=lease_ms,
        ),
        command_handler=DeterministicProductCommandHandler(),
        telegram_delivery=TelegramDeliveryAdapter(token_provider=telegram_token),
        ledger=DynamoProcessingLedger(
            dynamodb.Table(required["MESSAGE_LEDGER_TABLE_NAME"])
        ),
    )
    return _production_dependencies


def lambda_handler(event: Any, context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    dependencies = (
        _dependency_factory() if _dependency_factory else _build_production_dependencies()
    )
    return process_sqs_event(event, dependencies)
