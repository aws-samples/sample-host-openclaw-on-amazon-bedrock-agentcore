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
    from scheduler.models import SchedulePayloadV1
    from scheduler.service import SchedulerOutcome, SchedulerService
    from capabilities.contracts import ScheduleOccurrenceV1, ScheduleSpecV1
except ImportError:  # direct Lambda asset / focused tests
    from models import SchedulePayloadV1  # type: ignore[no-redef]
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


class DynamoIngressScheduleRepository:
    """Strong-read and idempotent occurrence subset used by the ingress."""

    def __init__(self, *, client, table_name: str) -> None:
        if not callable(getattr(client, "get_item", None)) or not callable(
            getattr(client, "put_item", None)
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
        if not isinstance(item, Mapping) or set(item) != {
            "PK",
            "SK",
            "recordJson",
            "deliveryJson",
        }:
            raise RuntimeError("scheduler control record is invalid")
        if (
            _string_attribute(item, "PK") != f"SCHEDULE#{schedule_id}"
            or _string_attribute(item, "SK") != "STATE"
        ):
            raise RuntimeError("scheduler control record crossed a schedule boundary")
        return item

    def strong_read_schedule(self, schedule_id: str) -> ScheduleSpecV1 | None:
        item = self._schedule_item(schedule_id)
        if item is None:
            return None
        try:
            record = json.loads(_string_attribute(item, "recordJson"))
            return ScheduleSpecV1.from_mapping(record)
        except (TypeError, ValueError):
            raise RuntimeError("scheduler control record is invalid") from None

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
        ):
            raise RuntimeError("scheduler delivery target is invalid")
        return dict(delivery)

    def put_occurrence_if_absent(self, occurrence: ScheduleOccurrenceV1) -> bool:
        if not isinstance(occurrence, ScheduleOccurrenceV1):
            raise TypeError("scheduler occurrence must be typed")
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
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
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
            return True
        except Exception as error:
            code = getattr(error, "response", {}).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
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
