"""EventBridge Scheduler target Lambda entry for the trusted scheduler.

The composition root creates no client at import time and holds no effect
authority of any kind. It parses the opaque fire payload (rejecting any user
content or extra keys) and delegates to ``SchedulerService.fire``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any, Callable, Mapping

try:  # package import
    from scheduler.models import SchedulePayloadV1, build_schedule_spec
    from scheduler.service import SchedulerOutcome, SchedulerService
    from capabilities.contracts import ScheduleOccurrenceV1, ScheduleSpecV1
except ImportError:  # direct Lambda asset / focused tests
    from models import SchedulePayloadV1, build_schedule_spec  # type: ignore[no-redef]
    from service import SchedulerOutcome, SchedulerService  # type: ignore[no-redef]
    from contracts import (  # type: ignore[no-redef]
        ScheduleOccurrenceV1,
        ScheduleSpecV1,
    )


REQUIRED_REGION = "eu-west-1"
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_QUEUE_URL = re.compile(
    r"https://sqs\.eu-west-1\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+\.fifo"
)
_RETENTION_SECONDS = 90 * 24 * 60 * 60

_service_factory: Callable[[], SchedulerService] | None = None
_production_service: SchedulerService | None = None


def _aws_client(service_name: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        service_name,
        region_name=REQUIRED_REGION,
        config=Config(retries={"max_attempts": 0}),
    )


def _string_attribute(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise RuntimeError("scheduler control record is invalid")
    text = value.get("S")
    if not isinstance(text, str):
        raise RuntimeError("scheduler control record is invalid")
    return text


def _integer_attribute(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"N"}:
        raise RuntimeError("scheduler control record is invalid")
    raw = value.get("N")
    if not isinstance(raw, str) or re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        raise RuntimeError("scheduler control record is invalid")
    return int(raw)


class DynamoIngressScheduleRepository:
    """Strong-read and idempotent occurrence subset used by the ingress."""

    def __init__(self, *, client, table_name: str) -> None:
        if any(
            not callable(getattr(client, method, None))
            for method in ("get_item", "transact_write_items")
        ):
            raise TypeError("scheduler ingress requires a DynamoDB client")
        if not isinstance(table_name, str) or _TABLE_NAME.fullmatch(table_name) is None:
            raise ValueError("scheduler control table name is invalid")
        self._client = client
        self._table_name = table_name

    @staticmethod
    def _key(schedule_id: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"SCHEDULE#{schedule_id}"}, "SK": {"S": "STATE"}}

    def _schedule_item(self, schedule_id: str) -> Mapping[str, Any] | None:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=self._key(schedule_id),
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("scheduler strong read returned an invalid response")
        item = response.get("Item")
        if item is None:
            return None
        required = {
            "PK",
            "SK",
            "userId",
            "scheduleUserId",
            "scheduleSortKey",
            "recordJson",
            "deliveryJson",
        }
        if not isinstance(item, Mapping) or set(item) not in (
            required,
            required | {"ttl"},
        ):
            raise RuntimeError("scheduler control record is invalid")
        if (
            _string_attribute(item, "PK") != f"SCHEDULE#{schedule_id}"
            or _string_attribute(item, "SK") != "STATE"
            or _string_attribute(item, "scheduleSortKey")
            != f"SCHEDULE#{schedule_id}"
        ):
            raise RuntimeError("scheduler control record crossed a schedule boundary")
        if "ttl" in item:
            _integer_attribute(item, "ttl")
        return item

    def strong_read_schedule(self, schedule_id: str) -> ScheduleSpecV1 | None:
        item = self._schedule_item(schedule_id)
        if item is None:
            return None
        try:
            record = json.loads(_string_attribute(item, "recordJson"))
            spec = ScheduleSpecV1.from_mapping(record)
        except (TypeError, ValueError):
            raise RuntimeError("scheduler control record is invalid") from None
        if (
            _string_attribute(item, "userId") != spec.user_id
            or _string_attribute(item, "scheduleUserId") != spec.user_id
        ):
            raise RuntimeError("scheduler control record crossed a user boundary")
        return spec

    def read_delivery_target(self, schedule_id: str) -> Mapping[str, Any] | None:
        item = self._schedule_item(schedule_id)
        if item is None:
            return None
        try:
            delivery = json.loads(_string_attribute(item, "deliveryJson"))
        except (TypeError, ValueError):
            raise RuntimeError("scheduler delivery target is invalid") from None
        if (
            not isinstance(delivery, Mapping)
            or set(delivery) != {"chatId", "actorId"}
            or not all(isinstance(delivery[name], str) for name in delivery)
            or delivery["actorId"] != f"telegram:{delivery['chatId']}"
        ):
            raise RuntimeError("scheduler delivery target is invalid")
        return dict(delivery)

    def put_occurrence_if_absent(
        self,
        spec: ScheduleSpecV1,
        occurrence: ScheduleOccurrenceV1,
        delivery_target: Mapping[str, Any],
    ) -> bool:
        if (
            not isinstance(spec, ScheduleSpecV1)
            or not isinstance(occurrence, ScheduleOccurrenceV1)
            or spec.state != "ENABLED"
            or spec.schedule_id != occurrence.schedule_id
            or spec.revision != occurrence.generation
            or spec.next_run_at != occurrence.occurrence_time
            or not isinstance(delivery_target, Mapping)
            or set(delivery_target) != {"chatId", "actorId"}
            or not all(
                isinstance(delivery_target[name], str)
                for name in ("chatId", "actorId")
            )
            or delivery_target["actorId"]
            != f"telegram:{delivery_target['chatId']}"
        ):
            raise TypeError("scheduler occurrence claim must be exactly typed")
        canonical_spec = json.dumps(
            spec.to_mapping(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        canonical_delivery = json.dumps(
            dict(delivery_target),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        occurrence_item = {
            "PK": {"S": f"SCHEDULE#{occurrence.schedule_id}"},
            "SK": {"S": f"OCCURRENCE#{occurrence.occurrence_id}"},
            "recordJson": {
                "S": json.dumps(
                    occurrence.to_mapping(),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
            "ttl": {
                "N": str(occurrence.occurrence_time + _RETENTION_SECONDS)
            },
            "deliveryState": {"S": "PENDING"},
            "terminal": {"BOOL": False},
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": self._key(spec.schedule_id),
                            "ConditionExpression": (
                                "recordJson = :record AND "
                                "deliveryJson = :delivery AND "
                                "userId = :user AND scheduleUserId = :user AND "
                                "scheduleSortKey = :sort"
                            ),
                            "ExpressionAttributeValues": {
                                ":record": {"S": canonical_spec},
                                ":delivery": {"S": canonical_delivery},
                                ":user": {"S": spec.user_id},
                                ":sort": {
                                    "S": f"SCHEDULE#{spec.schedule_id}"
                                },
                            },
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": occurrence_item,
                            "ConditionExpression": (
                                "attribute_not_exists(PK) AND "
                                "attribute_not_exists(SK)"
                            ),
                        }
                    },
                ]
            )
            return True
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {
                "ConditionalCheckFailedException",
                "TransactionCanceledException",
            }:
                return False
            raise

    def complete_occurrence(
        self,
        spec: ScheduleSpecV1,
        occurrence: ScheduleOccurrenceV1,
        *,
        now: int,
        delivery_state: str,
    ) -> bool:
        if (
            not isinstance(spec, ScheduleSpecV1)
            or not isinstance(occurrence, ScheduleOccurrenceV1)
            or spec.state != "ENABLED"
            or spec.schedule_id != occurrence.schedule_id
            or spec.revision != occurrence.generation
            or spec.next_run_at != occurrence.occurrence_time
            or isinstance(now, bool)
            or not isinstance(now, int)
            or now < 0
            or delivery_state not in {"ENQUEUED", "UNCERTAIN"}
        ):
            raise ValueError("scheduler occurrence completion binding is invalid")
        paused = build_schedule_spec(
            schedule_id=spec.schedule_id,
            user_id=spec.user_id,
            task_type=spec.task_type,
            definition=spec.definition,
            revision=spec.revision + 1,
            state="PAUSED",
            next_run_at=None,
        )
        terminal_occurrence = ScheduleOccurrenceV1.from_mapping(
            {
                **occurrence.to_mapping(),
                "status": "QUEUED" if delivery_state == "ENQUEUED" else "FAILED",
            }
        )
        canonical = lambda value: json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        ttl = occurrence.occurrence_time + _RETENTION_SECONDS
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": self._key(spec.schedule_id),
                            "UpdateExpression": "SET recordJson = :paused, ttl = :ttl",
                            "ConditionExpression": (
                                "recordJson = :current AND userId = :user"
                            ),
                            "ExpressionAttributeValues": {
                                ":current": {"S": canonical(spec.to_mapping())},
                                ":paused": {"S": canonical(paused.to_mapping())},
                                ":ttl": {"N": str(ttl)},
                                ":user": {"S": spec.user_id},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"SCHEDULE#{occurrence.schedule_id}"},
                                "SK": {
                                    "S": f"OCCURRENCE#{occurrence.occurrence_id}"
                                },
                            },
                            "UpdateExpression": (
                                "SET recordJson = :terminalRecord, "
                                "deliveryState = :delivery, terminal = :terminal"
                            ),
                            "ConditionExpression": (
                                "recordJson = :record AND deliveryState = :pending "
                                "AND terminal = :notTerminal"
                            ),
                            "ExpressionAttributeValues": {
                                ":record": {
                                    "S": canonical(occurrence.to_mapping())
                                },
                                ":terminalRecord": {
                                    "S": canonical(terminal_occurrence.to_mapping())
                                },
                                ":pending": {"S": "PENDING"},
                                ":delivery": {"S": delivery_state},
                                ":notTerminal": {"BOOL": False},
                                ":terminal": {"BOOL": True},
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": {
                                "PK": {"S": f"USER#{spec.user_id}"},
                                "SK": {"S": "CONTROL#SCHEDULE_COUNT"},
                            },
                            "UpdateExpression": "SET liveCount = liveCount - :one",
                            "ConditionExpression": (
                                "recordType = :counter AND userId = :user "
                                "AND liveCount > :zero"
                            ),
                            "ExpressionAttributeValues": {
                                ":one": {"N": "1"},
                                ":zero": {"N": "0"},
                                ":counter": {"S": "SCHEDULE_COUNTER"},
                                ":user": {"S": spec.user_id},
                            },
                        }
                    },
                ]
            )
            return True
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code in {"ConditionalCheckFailedException", "TransactionCanceledException"}:
                return False
            raise


class SqsOccurrenceQueue:
    def __init__(self, *, client, queue_url: str) -> None:
        if not callable(getattr(client, "send_message", None)):
            raise TypeError("scheduler ingress requires an SQS client")
        if not isinstance(queue_url, str) or _QUEUE_URL.fullmatch(queue_url) is None:
            raise ValueError("scheduler update queue URL is invalid")
        self._client = client
        self._queue_url = queue_url

    def send_occurrence(
        self, *, message_group_id: str, message_deduplication_id: str, body: str
    ) -> None:
        self._client.send_message(
            QueueUrl=self._queue_url,
            MessageGroupId=message_group_id,
            MessageDeduplicationId=message_deduplication_id,
            MessageBody=body,
        )


class _IngressOnlyScheduler:
    @staticmethod
    def create_one_time_schedule(**_kwargs) -> None:
        raise RuntimeError("ingress cannot create schedules")

    @staticmethod
    def delete_schedule(_schedule_id: str) -> None:
        raise RuntimeError("ingress cannot delete schedules")


def handle_fire(event: Any, service: SchedulerService) -> SchedulerOutcome:
    """Parse the opaque payload and delegate to the trusted fire path."""

    if isinstance(event, str):
        payload = SchedulePayloadV1.from_json(event)
    else:
        payload = SchedulePayloadV1.from_mapping(event)
    return service.fire(payload)


def configure_service_factory(
    factory: Callable[[], SchedulerService] | None,
) -> None:
    """Install (or clear) the deployment composition root."""

    global _service_factory, _production_service
    if factory is not None and not callable(factory):
        raise TypeError("scheduler service factory must be callable")
    _service_factory = factory
    _production_service = None


def build_scheduler_service(
    *, env: Mapping[str, str] = os.environ
) -> SchedulerService:
    """Create the exact-region trusted service only inside the deployed Lambda.

    No effect-plane client is ever constructed here. This function fails closed
    on region drift before creating any AWS resource.
    """

    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if region != REQUIRED_REGION:
        raise RuntimeError("scheduler ingress requires exact eu-west-1 region")
    table_name = env.get("SCHEDULER_CONTROL_TABLE_NAME")
    queue_url = env.get("SCHEDULER_UPDATE_QUEUE_URL")
    repository = DynamoIngressScheduleRepository(
        client=_aws_client("dynamodb"),
        table_name=table_name,
    )
    queue = SqsOccurrenceQueue(client=_aws_client("sqs"), queue_url=queue_url)
    return SchedulerService(
        repository=repository,
        scheduler=_IngressOnlyScheduler(),
        queue=queue,
        clock=lambda: int(time.time()),
        nonce_factory=lambda: secrets.token_urlsafe(24),
        uncertain_errors=(Exception,),
    )


def lambda_handler(event: Any, _context: Any) -> dict[str, str]:
    global _production_service
    if _service_factory is not None:
        service = _service_factory()
    else:
        if _production_service is None:
            _production_service = build_scheduler_service()
        service = _production_service
    outcome = handle_fire(event, service)
    return {"status": outcome.status}


__all__ = [
    "REQUIRED_REGION",
    "DynamoIngressScheduleRepository",
    "SqsOccurrenceQueue",
    "build_scheduler_service",
    "configure_service_factory",
    "handle_fire",
    "lambda_handler",
]
