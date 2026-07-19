"""Proposal-only DynamoDB port for the frozen schedule capability surface."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from botocore.exceptions import ClientError

from scheduler.proposals import (
    PHYSICAL_RETENTION_SECONDS,
    ScheduleProposalRecordV1,
    build_cancel_schedule_proposal,
    build_create_schedule_proposal,
)

from .contracts import ScheduleSpecV1, canonical_json_bytes


_TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SCHEDULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_INVOCATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_TELEGRAM_CHAT = re.compile(r"[1-9][0-9]{0,19}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_SCHEDULES = 256


def _string_attribute(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise RuntimeError("scheduler control record is invalid")
    text = value.get("S")
    if not isinstance(text, str):
        raise RuntimeError("scheduler control record is invalid")
    return text


class DynamoScheduleCapabilityPort:
    """Strong-read schedules and durably prepare proposals, never dispatch."""

    def __init__(
        self,
        *,
        client: Any,
        table_name: str,
        authority_table_name: str,
        catalog_digest: str,
        clock: Callable[[], int],
        nonce_factory: Callable[[], str],
    ) -> None:
        if any(
            not callable(getattr(client, method, None))
            for method in ("query", "get_item", "put_item")
        ):
            raise TypeError("schedule capability port requires exact Dynamo methods")
        if not isinstance(table_name, str) or _TABLE.fullmatch(table_name) is None:
            raise ValueError("schedule capability table name is invalid")
        if (
            not isinstance(authority_table_name, str)
            or _TABLE.fullmatch(authority_table_name) is None
        ):
            raise ValueError("schedule capability authority table name is invalid")
        if not isinstance(catalog_digest, str) or _SHA256.fullmatch(catalog_digest) is None:
            raise ValueError("schedule capability catalog digest is invalid")
        if not callable(clock) or not callable(nonce_factory):
            raise TypeError("schedule capability port requires trusted time and nonce")
        self._client = client
        self._table_name = table_name
        self._authority_table_name = authority_table_name
        self._catalog_digest = catalog_digest
        self._clock = clock
        self._nonce_factory = nonce_factory

    @staticmethod
    def _user(user_id: str) -> str:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ValueError("schedule capability user is invalid")
        return user_id

    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("schedule capability clock is invalid")
        return value

    def _delivery(self, user_id: str, invocation_id: str) -> Mapping[str, str]:
        if (
            not isinstance(invocation_id, str)
            or _INVOCATION.fullmatch(invocation_id) is None
        ):
            raise ValueError("schedule proposal invocation is invalid")
        response = self._client.get_item(
            TableName=self._authority_table_name,
            Key={
                "PK": {"S": f"TURN#{invocation_id}"},
                "SK": {"S": "DELIVERY"},
            },
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if not isinstance(raw, Mapping) or set(raw) != {
            "PK",
            "SK",
            "recordJson",
            "version",
        }:
            raise RuntimeError("schedule proposal delivery context is unavailable")
        if (
            _string_attribute(raw, "PK") != f"TURN#{invocation_id}"
            or _string_attribute(raw, "SK") != "DELIVERY"
            or raw.get("version") != {"N": "1"}
        ):
            raise RuntimeError("schedule proposal delivery context is invalid")
        try:
            record = json.loads(_string_attribute(raw, "recordJson"))
        except (TypeError, ValueError):
            raise RuntimeError("schedule proposal delivery context is invalid") from None
        if (
            not isinstance(record, Mapping)
            or set(record)
            != {
                "schema",
                "userId",
                "invocationId",
                "channel",
                "actorId",
                "chatId",
            }
            or record.get("schema")
            != "personal-operator.turn-delivery-context.v1"
            or record.get("userId") != user_id
            or record.get("invocationId") != invocation_id
            or record.get("channel") != "telegram"
            or not isinstance(record.get("chatId"), str)
            or _TELEGRAM_CHAT.fullmatch(record["chatId"]) is None
            or record.get("actorId") != f"telegram:{record['chatId']}"
        ):
            raise RuntimeError("schedule proposal delivery context is invalid")
        return {"actorId": record["actorId"], "chatId": record["chatId"]}

    def _read_schedule(self, schedule_id: str) -> ScheduleSpecV1 | None:
        if not isinstance(schedule_id, str) or _SCHEDULE.fullmatch(schedule_id) is None:
            raise ValueError("schedule identity is invalid")
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"SCHEDULE#{schedule_id}"},
                "SK": {"S": "STATE"},
            },
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or set(raw) != {
            "PK",
            "SK",
            "userId",
            "scheduleUserId",
            "scheduleSortKey",
            "recordJson",
            "deliveryJson",
        }:
            raise RuntimeError("scheduler control record is invalid")
        if (
            _string_attribute(raw, "PK") != f"SCHEDULE#{schedule_id}"
            or _string_attribute(raw, "SK") != "STATE"
            or _string_attribute(raw, "scheduleSortKey")
            != f"SCHEDULE#{schedule_id}"
        ):
            raise RuntimeError("scheduler control record crossed a schedule boundary")
        try:
            spec = ScheduleSpecV1.from_mapping(
                json.loads(_string_attribute(raw, "recordJson"))
            )
        except (TypeError, ValueError):
            raise RuntimeError("scheduler control record is invalid") from None
        if (
            spec.schedule_id != schedule_id
            or _string_attribute(raw, "userId") != spec.user_id
            or _string_attribute(raw, "scheduleUserId") != spec.user_id
        ):
            raise RuntimeError("scheduler control record binding is invalid")
        return spec

    def list_view(self, user_id: str) -> Mapping[str, Any]:
        user_id = self._user(user_id)
        response = self._client.query(
            TableName=self._table_name,
            IndexName="schedule-user-index-v1",
            KeyConditionExpression="#user = :user",
            ExpressionAttributeNames={"#user": "scheduleUserId"},
            ExpressionAttributeValues={":user": {"S": user_id}},
            ProjectionExpression="PK, SK, scheduleUserId, scheduleSortKey",
            Limit=_MAX_SCHEDULES + 1,
        )
        raw_items = response.get("Items")
        if (
            not isinstance(raw_items, list)
            or response.get("LastEvaluatedKey") is not None
            or len(raw_items) > _MAX_SCHEDULES
        ):
            raise RuntimeError("schedule listing exceeds its bound")
        schedules = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, Mapping) or set(raw) != {
                "PK",
                "SK",
                "scheduleUserId",
                "scheduleSortKey",
            }:
                raise RuntimeError("schedule index record is invalid")
            pk = _string_attribute(raw, "PK")
            sk = _string_attribute(raw, "SK")
            if _string_attribute(raw, "scheduleUserId") != user_id:
                raise RuntimeError("schedule index crossed a tenant boundary")
            if not pk.startswith("SCHEDULE#") or sk != "STATE":
                continue
            schedule_id = pk.removeprefix("SCHEDULE#")
            if _string_attribute(raw, "scheduleSortKey") != pk:
                raise RuntimeError("schedule index sort binding is invalid")
            if schedule_id in seen:
                raise RuntimeError("schedule index contains a duplicate")
            spec = self._read_schedule(schedule_id)
            if spec is None or spec.user_id != user_id:
                raise RuntimeError("schedule listing authority changed during read")
            seen.add(schedule_id)
            schedules.append(
                {
                    "scheduleId": spec.schedule_id,
                    "taskType": spec.task_type,
                    "state": spec.state,
                    "nextRunAt": spec.next_run_at,
                }
            )
        schedules.sort(key=lambda item: item["scheduleId"])
        return {"schedules": schedules}

    def _put_proposal(
        self, record: ScheduleProposalRecordV1
    ) -> Mapping[str, Any]:
        if not isinstance(record, ScheduleProposalRecordV1):
            raise TypeError("schedule proposal must use the shared contract")
        proposal_ref = record.proposal_id
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    "PK": {"S": f"USER#{record.user_id}"},
                    "SK": {"S": f"PROPOSAL#{proposal_ref}"},
                    "proposalUserId": {"S": record.user_id},
                    "proposalSortKey": {
                        "S": f"{record.created_at:020d}#{proposal_ref}"
                    },
                    "recordJson": {
                        "S": canonical_json_bytes(record.to_mapping()).decode("utf-8")
                    },
                    "state": {"S": "PENDING"},
                    "version": {"N": "1"},
                    "ttl": {
                        "N": str(record.created_at + PHYSICAL_RETENTION_SECONDS)
                    },
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == (
                "ConditionalCheckFailedException"
            ):
                raise RuntimeError("schedule proposal identity collided") from error
            raise
        return {
            "proposalRef": proposal_ref,
            "expiresAt": record.expires_at,
        }

    def propose(
        self,
        *,
        user_id: str,
        invocation_id: str,
        task_type: str,
        definition: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        user_id = self._user(user_id)
        delivery = self._delivery(user_id, invocation_id)
        now = self._now()
        return self._put_proposal(
            build_create_schedule_proposal(
                catalog_digest=self._catalog_digest,
                user_id=user_id,
                invocation_id=invocation_id,
                task_type=task_type,
                definition=definition,
                delivery_target=delivery,
                now=now,
                nonce=self._nonce_factory(),
            )
        )

    def cancel_propose(
        self,
        *,
        user_id: str,
        invocation_id: str,
        schedule_id: str,
    ) -> Mapping[str, Any]:
        user_id = self._user(user_id)
        spec = self._read_schedule(schedule_id)
        if spec is None or spec.user_id != user_id:
            raise RuntimeError("schedule is unavailable for cancellation")
        delivery = self._delivery(user_id, invocation_id)
        now = self._now()
        return self._put_proposal(
            build_cancel_schedule_proposal(
                catalog_digest=self._catalog_digest,
                user_id=user_id,
                invocation_id=invocation_id,
                schedule_id=schedule_id,
                revision=spec.revision,
                delivery_target=delivery,
                now=now,
                nonce=self._nonce_factory(),
            )
        )


__all__ = ["DynamoScheduleCapabilityPort"]
