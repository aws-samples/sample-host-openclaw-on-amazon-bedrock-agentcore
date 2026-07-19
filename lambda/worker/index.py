"""Trusted ordered Telegram worker.

Provider credentials belong to the injected delivery adapter. This module
passes only a minimal allowlisted request to ``RuntimeDriver`` and implements
SQS FIFO partial-batch failure semantics without importing an AWS SDK.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

try:
    from router.message_queue import QueueEnvelope
    from router.product_commands import ProductCommand, parse_product_command
except ImportError:  # focused tests add lambda/router directly to sys.path
    from message_queue import QueueEnvelope
    from product_commands import ProductCommand, parse_product_command

try:
    from worker.telegram_delivery import (
        TELEGRAM_MAX_HTML_CHARS,
        TelegramDeliveryValidationError,
        render_safe_telegram_html,
    )
except ImportError:
    from telegram_delivery import (
        TELEGRAM_MAX_HTML_CHARS,
        TelegramDeliveryValidationError,
        render_safe_telegram_html,
    )

try:
    from worker.telegram_cards import (
        TelegramCardValidationError,
        TelegramCommandResult,
        decode_ledger_result,
        encode_ledger_result,
    )
except ImportError:
    from telegram_cards import (
        TelegramCardValidationError,
        TelegramCommandResult,
        decode_ledger_result,
        encode_ledger_result,
    )


try:
    from capabilities.contracts import derive_occurrence_id
except ImportError:  # focused tests add lambda/capabilities directly to sys.path
    from contracts import derive_occurrence_id


logger = logging.getLogger(__name__)

_LOG_SCHEMA = "personal-operator.log.v1"
_LOG_WARNING_EVENTS = frozenset(
    {"callback_acknowledgement_failed", "fifo_record_failed"}
)


def _log_warning(event: str) -> None:
    """Emit one closed metadata record without source or exception data."""

    if event not in _LOG_WARNING_EVENTS:
        raise ValueError("worker log event is not allowlisted")
    logger.warning(
        json.dumps(
            {
                "component": "worker",
                "event": event,
                "level": "WARNING",
                "schema": _LOG_SCHEMA,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )

MAX_RUNTIME_RESPONSE_CHARS = 3_500
MAX_TELEGRAM_HTML_CHARS = TELEGRAM_MAX_HTML_CHARS
MAX_SQS_BATCH_SIZE = 10
MAX_OCCURRENCE_BODY_BYTES = 16 * 1024

OCCURRENCE_BODY_SCHEMA = "personal-operator.schedule-occurrence-body.v1"
_OCCURRENCE_FIELDS = frozenset(
    {
        "schema",
        "occurrenceId",
        "scheduleId",
        "userId",
        "generation",
        "occurrenceTime",
        "taskType",
        "chatId",
        "actorId",
        "scheduled",
        "externalEffects",
    }
)
_OCCURRENCE_TASK_TYPES = frozenset({"REMINDER", "READ_ONLY_AGENT_TURN"})
_OCCURRENCE_USER_ID = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_OCCURRENCE_SCHEDULE_ID = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_OCCURRENCE_ID = __import__("re").compile(r"occ_[0-9a-f]{64}")
_OCCURRENCE_CHAT_ID = __import__("re").compile(r"[1-9][0-9]{0,19}")
_OCCURRENCE_ACTOR_ID = __import__("re").compile(r"telegram:([1-9][0-9]{0,19})")
_MAX_SAFE_INTEGER = 2**53 - 1


class WorkerContractError(RuntimeError):
    """A dependency returned data that cannot cross the trusted boundary."""


class ScheduledOccurrence:
    """A distinguishable scheduler-enqueued occurrence parsed before QueueEnvelope.

    It duck-types the ledger's envelope contract (user_id, channel, trace_id,
    request_sha256, message_group_id, message_deduplication_id, payload) so the
    at-most-once claim/result/delivery state machine is reused verbatim. Its
    kind is ``occurrence`` and it carries the read-only markers scheduled=True /
    externalEffects=False, so a scheduled READ_ONLY_AGENT_TURN can only read or
    prepare a fresh proposal, never dispatch a connector/browser effect.
    """

    __slots__ = (
        "user_id",
        "channel",
        "kind",
        "schedule_id",
        "occurrence_id",
        "generation",
        "occurrence_time",
        "task_type",
        "chat_id",
        "actor_id",
        "content",
        "trace_id",
        "_request_sha256",
    )

    def __init__(self, body: Mapping[str, Any]) -> None:
        if not isinstance(body, Mapping):
            raise WorkerContractError("scheduled occurrence body must be an object")
        task_type = body.get("taskType")
        if task_type not in _OCCURRENCE_TASK_TYPES:
            raise WorkerContractError("scheduled occurrence task type is unsupported")
        content_field = "message" if task_type == "REMINDER" else "prompt"
        # Exactly the fixed envelope plus the single content field for the type;
        # the opposite content field or any extra key is rejected.
        expected = _OCCURRENCE_FIELDS | {content_field}
        if set(body) != expected:
            raise WorkerContractError("scheduled occurrence body is not exact")
        if body.get("schema") != OCCURRENCE_BODY_SCHEMA:
            raise WorkerContractError("scheduled occurrence schema is invalid")
        if body.get("scheduled") is not True or body.get("externalEffects") is not False:
            raise WorkerContractError("scheduled occurrence markers are invalid")
        user_id = body.get("userId")
        schedule_id = body.get("scheduleId")
        occurrence_id = body.get("occurrenceId")
        generation = body.get("generation")
        occurrence_time = body.get("occurrenceTime")
        chat_id = body.get("chatId")
        actor_id = body.get("actorId")
        content = body.get(content_field)
        if (
            not isinstance(user_id, str)
            or _OCCURRENCE_USER_ID.fullmatch(user_id) is None
            or not isinstance(schedule_id, str)
            or _OCCURRENCE_SCHEDULE_ID.fullmatch(schedule_id) is None
            or not isinstance(occurrence_id, str)
            or _OCCURRENCE_ID.fullmatch(occurrence_id) is None
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= _MAX_SAFE_INTEGER
            or isinstance(occurrence_time, bool)
            or not isinstance(occurrence_time, int)
            or not 0 <= occurrence_time <= _MAX_SAFE_INTEGER
            or not isinstance(chat_id, str)
            or _OCCURRENCE_CHAT_ID.fullmatch(chat_id) is None
            or not isinstance(actor_id, str)
            or _OCCURRENCE_ACTOR_ID.fullmatch(actor_id) is None
            or not isinstance(content, str)
            or not content
            or len(content) > 4096
        ):
            raise WorkerContractError("scheduled occurrence body is invalid")
        if chat_id != actor_id.removeprefix("telegram:"):
            raise WorkerContractError("scheduled occurrence chat is not its actor")
        # The occurrence id must bind schedule + generation + time exactly.
        if occurrence_id != derive_occurrence_id(
            schedule_id, generation, occurrence_time
        ):
            raise WorkerContractError(
                "scheduled occurrence id does not bind its schedule generation and time"
            )
        self.user_id = user_id
        self.channel = "telegram"
        self.kind = "occurrence"
        self.schedule_id = schedule_id
        self.occurrence_id = occurrence_id
        self.generation = generation
        self.occurrence_time = occurrence_time
        self.task_type = task_type
        self.chat_id = chat_id
        self.actor_id = actor_id
        self.content = content
        # The occurrence id is the durable event identity: deterministic across
        # duplicate fires so the ledger collapses replays to at-most-once.
        self.trace_id = occurrence_id
        self._request_sha256 = hashlib.sha256(
            b"personal-operator-schedule-occurrence-body-v1\0"
            + occurrence_id.encode("utf-8")
        ).hexdigest()

    @property
    def payload(self) -> dict[str, Any]:
        return {"chatId": self.chat_id, "actorId": self.actor_id}

    @property
    def message_group_id(self) -> str:
        return self.user_id

    @property
    def message_deduplication_id(self) -> str:
        return self.occurrence_id

    @property
    def request_sha256(self) -> str:
        return self._request_sha256

    @classmethod
    def from_json(cls, body: Any) -> "ScheduledOccurrence":
        if (
            not isinstance(body, str)
            or not body
            or len(body.encode("utf-8")) > MAX_OCCURRENCE_BODY_BYTES
        ):
            raise WorkerContractError("occurrence body must be bounded UTF-8 JSON text")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise WorkerContractError("duplicate JSON key")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                body,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    WorkerContractError(token)
                ),
            )
        except WorkerContractError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WorkerContractError("occurrence body is not valid JSON") from error
        return cls(parsed)


def _looks_like_occurrence_body(body: Any) -> bool:
    if not isinstance(body, str) or f'"{OCCURRENCE_BODY_SCHEMA}"' not in body:
        return False
    try:
        peeked = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(peeked, Mapping) and peeked.get("schema") == OCCURRENCE_BODY_SCHEMA


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
        chat_id: str,
        actor_id: str,
    ) -> str | TelegramCommandResult: ...

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
    ) -> str | TelegramCommandResult: ...


class TelegramDelivery(Protocol):
    def acknowledge_callback(self, *, callback_query_id: str) -> None: ...

    def send_message(
        self,
        *,
        chat_id: str,
        html: str,
        reply_markup: Mapping[str, Any] | None,
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


class DeletionFence(Protocol):
    def is_account_deleted(self, user_id: str) -> bool: ...


class ControlDeletionFence(Protocol):
    def deletion_blocked(
        self,
        *,
        user_id: str,
        channel: str,
        trace_id: str,
        idempotency_key: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkerDependencies:
    runtime_driver: RuntimeDriver
    command_handler: ProductCommandHandler
    telegram_delivery: TelegramDelivery
    ledger: ProcessingLedger
    control_deletion_fence: ControlDeletionFence
    deletion_fence: DeletionFence


def render_telegram_html(text: str) -> str:
    """Escape runtime text, then enable only a small Telegram-safe subset."""

    if not isinstance(text, str) or not text or len(text) > MAX_RUNTIME_RESPONSE_CHARS:
        raise WorkerContractError("runtime response must be non-empty bounded text")
    try:
        return render_safe_telegram_html(text)
    except TelegramDeliveryValidationError as error:
        raise WorkerContractError(
            "rendered Telegram message exceeds one safe send"
        ) from error


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
        chat_id=envelope.payload["chatId"],
        actor_id=envelope.payload["actorId"],
    )
    try:
        return encode_ledger_result(result)
    except (TypeError, ValueError, TelegramCardValidationError) as error:
        raise WorkerContractError("command handler returned an invalid result") from error


def _callback_result(
    envelope: QueueEnvelope,
    idempotency_key: str,
    dependencies: WorkerDependencies,
) -> str:
    payload = envelope.payload
    handler = getattr(dependencies.command_handler, "handle_callback", None)
    if not callable(handler):
        raise WorkerContractError("callback control handler is unavailable")
    result = handler(
        user_id=envelope.user_id,
        channel=envelope.channel,
        trace_id=envelope.trace_id,
        idempotency_key=idempotency_key,
        chat_id=payload["chatId"],
        actor_id=payload["actorId"],
        callback_data=payload["callbackData"],
    )
    try:
        return encode_ledger_result(result)
    except (TypeError, ValueError, TelegramCardValidationError) as error:
        raise WorkerContractError("callback handler returned an invalid result") from error


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


def _occurrence_result(
    occurrence: "ScheduledOccurrence",
    dependencies: WorkerDependencies,
) -> str:
    """Compute the durable result text for one scheduled occurrence.

    A REMINDER renders its fixed reminder text (a Telegram notification, not a
    connector effect). A READ_ONLY_AGENT_TURN invokes the runtime with the
    explicit read-only markers so the turn's capability grant can only read or
    PREPARE a fresh proposal and can never dispatch an external effect.
    """

    if occurrence.task_type == "REMINDER":
        return occurrence.content
    if occurrence.task_type == "READ_ONLY_AGENT_TURN":
        # The request carries ONLY the fields the runtime contract allows
        # ({message, actorId, channel}); the read-only authority is a trusted,
        # server-set invoke parameter (never a caller-controlled request field),
        # so the runtime's capability grant is confined to read/propose and can
        # never dispatch a connector/browser effect.
        request = {
            "channel": occurrence.channel,
            "actorId": occurrence.actor_id,
            "message": occurrence.content,
        }
        result = dependencies.runtime_driver.invoke(
            occurrence.user_id,
            request,
            occurrence.trace_id,
            scheduled_read_only=True,
        )
        return _extract_runtime_text(result)
    raise WorkerContractError("scheduled occurrence task type is unsupported")


def process_envelope(envelope: QueueEnvelope, dependencies: WorkerDependencies) -> None:
    """Process one update with durable execution and at-most-one delivery attempt."""

    if not isinstance(envelope, (QueueEnvelope, ScheduledOccurrence)):
        raise TypeError("envelope must be a QueueEnvelope or ScheduledOccurrence")
    if not isinstance(dependencies, WorkerDependencies):
        raise TypeError("dependencies must be WorkerDependencies")

    # Telegram's UI acknowledgement is deliberately outside the business
    # ledger. It may repeat on SQS replay and its failure must never suppress
    # or duplicate the exactly-once callback transition below.
    if envelope.kind == "callback":
        callback_query_id = envelope.payload.get("callbackQueryId")
        if callback_query_id is not None:
            try:
                acknowledge = getattr(
                    dependencies.telegram_delivery,
                    "acknowledge_callback",
                )
                acknowledge(callback_query_id=callback_query_id)
            except Exception:
                _log_warning("callback_acknowledgement_failed")

    # The durable control intent is the first deletion authority and can exist
    # even when runtime purge failed. Check it synchronously through the exact
    # control alias before creating any new message-ledger state.
    if dependencies.control_deletion_fence.deletion_blocked(
        user_id=envelope.user_id,
        channel=envelope.channel,
        trace_id=envelope.trace_id,
        idempotency_key=envelope.message_deduplication_id,
    ):
        return
    # Keep the runtime tombstone as defense in depth and as the permanent fence
    # after the control-table deletion intent itself reaches completion.
    if dependencies.deletion_fence.is_account_deleted(envelope.user_id):
        return

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
            elif envelope.kind == "callback":
                proposed_result = _callback_result(
                    envelope, envelope.message_deduplication_id, dependencies
                )
            elif envelope.kind == "occurrence":
                proposed_result = _occurrence_result(envelope, dependencies)
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
    try:
        delivery_result = (
            decode_ledger_result(result)
            if envelope.kind in {"command", "callback"}
            else TelegramCommandResult(text=result)
        )
    except (TypeError, ValueError, TelegramCardValidationError) as error:
        raise WorkerContractError("ledger returned an invalid Telegram result") from error
    # Rendering is deterministic and validated before the durable delivery
    # claim. A pathological escaped payload therefore cannot consume the
    # at-most-one provider-attempt fence without making a network call.
    rendered = render_telegram_html(delivery_result.text)

    delivery_claim = dependencies.ledger.begin_delivery(claim, owner=owner)
    delivery_state = getattr(delivery_claim, "state", None)
    if delivery_state == "DELIVERED":
        return
    if delivery_state != "DELIVERY_CLAIMED":
        raise WorkerContractError(
            "delivery is already in flight or uncertain and will not be sent again"
        )

    try:
        # A command/runtime turn can take minutes. Re-read the first durable
        # deletion authority after claiming the outbox and immediately before
        # the provider call. If deletion won the race, quarantine this claimed
        # delivery without making a network request.
        if dependencies.control_deletion_fence.deletion_blocked(
            user_id=envelope.user_id,
            channel=envelope.channel,
            trace_id=envelope.trace_id,
            idempotency_key=envelope.message_deduplication_id,
        ):
            dependencies.ledger.mark_delivery_uncertain(
                delivery_claim, error_type="AccountDeletionFence"
            )
            return
        receipt = dependencies.telegram_delivery.send_message(
            chat_id=envelope.payload["chatId"],
            html=rendered,
            reply_markup=delivery_result.reply_markup(),
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


def _envelope_from_sqs_record(record: Any):
    if not isinstance(record, Mapping) or record.get("eventSource") != "aws:sqs":
        raise WorkerContractError("worker accepts only SQS event records")
    body = record.get("body")
    # A scheduler-enqueued occurrence rides the same per-user FIFO as a
    # distinguishable SQS body. Detect and parse it before QueueEnvelope so the
    # message_queue contract need not change to carry a new envelope kind.
    if _looks_like_occurrence_body(body):
        envelope = ScheduledOccurrence.from_json(body)
    else:
        envelope = QueueEnvelope.from_json(body)
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
        except Exception:  # AWS needs a partial-batch response for all retryable failures.
            _log_warning("fifo_record_failed")
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
            "AGENTCORE_RUNTIME_IAM_ARN",
            "AGENTCORE_QUALIFIER",
            "CAPABILITY_STATE_TABLE_NAME",
            "CAPABILITY_RELEASE_COMMIT",
            "CAPABILITY_CATALOG_DIGEST",
            "RUNTIME_STATE_TABLE_NAME",
            "MESSAGE_LEDGER_TABLE_NAME",
            "RUNTIME_LEASE_MS",
            "LAMBDA_TIMEOUT_SECONDS",
            "TELEGRAM_TOKEN_SECRET_ID",
            "CONTROL_FUNCTION_NAME",
            "WORKSPACE_CAPABILITY_SECRET_ID",
            "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"worker configuration missing: {','.join(sorted(missing))}")

    import boto3
    from botocore.config import Config
    from capabilities.composition import load_packaged_catalog
    from capabilities.durable import DynamoTurnAuthorityRepository
    from capabilities.issuer import TurnCapabilityIssuer
    try:
        from router.runtime_driver import AgentCoreAdapter, RuntimeDriver
        from router.runtime_state import RuntimeStateRepository
        from router.workspace_capability import WorkspaceCapabilitySigner
        from worker.control_client import LambdaProductCommandHandler
        from worker.deletion_fence import RuntimeAccountDeletionFence
        from worker.processing_ledger import DynamoProcessingLedger
        from worker.telegram_delivery import TelegramDeliveryAdapter
    except ImportError:  # direct lambda/worker asset is unsupported but testable
        from runtime_driver import AgentCoreAdapter, RuntimeDriver
        from runtime_state import RuntimeStateRepository
        from workspace_capability import WorkspaceCapabilitySigner
        from control_client import LambdaProductCommandHandler
        from deletion_fence import RuntimeAccountDeletionFence
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
    control_lambda = boto3.client(
        "lambda",
        region_name=region,
        config=Config(
            connect_timeout=10,
            read_timeout=180,
            retries={"max_attempts": 0},
        ),
    )
    token_cache: list[str] = []
    workspace_capability_key_cache: list[str] = []

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

    def workspace_capability_key() -> str:
        if workspace_capability_key_cache:
            return workspace_capability_key_cache[0]
        response = secrets.get_secret_value(
            SecretId=required["WORKSPACE_CAPABILITY_SECRET_ID"]
        )
        value = response.get("SecretString")
        if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
            raise RuntimeError("workspace capability secret is unavailable")
        workspace_capability_key_cache.append(value)
        return value

    try:
        lease_ms = int(required["RUNTIME_LEASE_MS"])
        maximum_execution_ms = int(required["LAMBDA_TIMEOUT_SECONDS"]) * 1_000
    except ValueError as error:
        raise RuntimeError("worker runtime authority must be integral") from error
    if maximum_execution_ms <= 0 or lease_ms <= maximum_execution_ms:
        raise RuntimeError("worker lease must outlive Lambda execution authority")
    runtime_repository = RuntimeStateRepository(
        dynamodb.Table(required["RUNTIME_STATE_TABLE_NAME"]),
        runtime_arn=required["AGENTCORE_RUNTIME_ARN"],
        runtime_qualifier=required["AGENTCORE_QUALIFIER"],
    )
    control_handler = LambdaProductCommandHandler(
        control_lambda,
        function_name=required["CONTROL_FUNCTION_NAME"],
    )
    capability_catalog = load_packaged_catalog(os.environ)
    turn_capability_issuer = TurnCapabilityIssuer(
        catalog=capability_catalog,
        authority_repository=DynamoTurnAuthorityRepository(
            client=dynamodb.meta.client,
            table_name=required["CAPABILITY_STATE_TABLE_NAME"],
            catalog=capability_catalog,
        ),
        runtime_arn=required["AGENTCORE_RUNTIME_IAM_ARN"],
        runtime_qualifier=required["AGENTCORE_QUALIFIER"],
        clock=lambda: int(time.time()),
        nonce_factory=lambda: f"nonce_{uuid.uuid4().hex}",
    )
    _production_dependencies = WorkerDependencies(
        runtime_driver=RuntimeDriver(
            repository=runtime_repository,
            adapter=AgentCoreAdapter(
                agentcore,
                runtime_arn=required["AGENTCORE_RUNTIME_ARN"],
                qualifier=required["AGENTCORE_QUALIFIER"],
                region=region,
            ),
            workspace_capability_signer=WorkspaceCapabilitySigner(
                key_provider=workspace_capability_key,
                audience=required[
                    "WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME"
                ],
            ),
            turn_capability_issuer=turn_capability_issuer,
            lease_ms=lease_ms,
            max_execution_ms=maximum_execution_ms,
        ),
        command_handler=control_handler,
        telegram_delivery=TelegramDeliveryAdapter(token_provider=telegram_token),
        ledger=DynamoProcessingLedger(
            dynamodb.Table(required["MESSAGE_LEDGER_TABLE_NAME"])
        ),
        control_deletion_fence=control_handler,
        deletion_fence=RuntimeAccountDeletionFence(runtime_repository),
    )
    return _production_dependencies


def lambda_handler(event: Any, context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    dependencies = (
        _dependency_factory() if _dependency_factory else _build_production_dependencies()
    )
    return process_sqs_event(event, dependencies)
