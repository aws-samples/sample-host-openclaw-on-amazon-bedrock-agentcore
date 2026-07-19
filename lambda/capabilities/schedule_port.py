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
from .retention import derive_deletion_subject_binding, subject_partition_key


_TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SCHEDULE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_INVOCATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_TELEGRAM_CHAT = re.compile(r"[1-9][0-9]{0,19}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_SCHEDULES = 256
_PORTABLE_PROJECTION_SCHEMA = (
    "personal-operator.portable-schedule-projection.v1"
)


def _string_attribute(item: Mapping[str, Any], name: str) -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {"S"}:
        raise RuntimeError("scheduler control record is invalid")
    text = value.get("S")
    if not isinstance(text, str):
        raise RuntimeError("scheduler control record is invalid")
    return text


def _positive_number_attribute(item: Mapping[str, Any], name: str) -> int:
    value = item.get(name)
    if (
        not isinstance(value, Mapping)
        or set(value) != {"N"}
        or not isinstance(value.get("N"), str)
        or not value["N"].isdigit()
        or int(value["N"]) < 1
    ):
        raise RuntimeError("portable schedule projection is invalid")
    return int(value["N"])


class DynamoPortableScheduleProjectionReader:
    """Read only the content-free projection atomically stored at activation."""

    _FIELDS = {
        "PK",
        "SK",
        "recordType",
        "userId",
        "generation",
        "liveBundleHash",
        "liveScheduleProjectionJson",
    }

    def __init__(self, *, client: Any, table_name: str) -> None:
        if not callable(getattr(client, "get_item", None)):
            raise TypeError("portable schedule reader requires exact Dynamo reads")
        if not isinstance(table_name, str) or _TABLE.fullmatch(table_name) is None:
            raise ValueError("portable schedule table name is invalid")
        self._client = client
        self._table_name = table_name

    def disabled_schedule_views(self, user_id: str) -> list[dict[str, str]]:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ValueError("schedule capability user is invalid")
        response = self._client.get_item(
            TableName=self._table_name,
            Key={
                "PK": {"S": f"USER#{user_id}"},
                "SK": {"S": "PORTABLE#LIVE_STATE"},
            },
            ProjectionExpression=(
                "PK, SK, #recordType, #userId, #generation, "
                "liveBundleHash, liveScheduleProjectionJson"
            ),
            ExpressionAttributeNames={
                "#recordType": "recordType",
                "#userId": "userId",
                "#generation": "generation",
            },
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("portable schedule projection read is invalid")
        raw = response.get("Item")
        if raw is None:
            return []
        if not isinstance(raw, Mapping) or set(raw) != self._FIELDS:
            raise RuntimeError("portable schedule projection is invalid")
        generation = _positive_number_attribute(raw, "generation")
        bundle_hash = _string_attribute(raw, "liveBundleHash")
        projection_json = _string_attribute(raw, "liveScheduleProjectionJson")
        if (
            _string_attribute(raw, "PK") != f"USER#{user_id}"
            or _string_attribute(raw, "SK") != "PORTABLE#LIVE_STATE"
            or _string_attribute(raw, "recordType") != "PORTABLE_LIVE_STATE_V2"
            or _string_attribute(raw, "userId") != user_id
            or _SHA256.fullmatch(bundle_hash) is None
        ):
            raise RuntimeError("portable schedule projection is invalid")
        try:
            projection = json.loads(projection_json)
            if canonical_json_bytes(projection).decode("utf-8") != projection_json:
                raise ValueError("noncanonical projection")
        except (TypeError, ValueError):
            raise RuntimeError("portable schedule projection is invalid") from None
        if (
            not isinstance(projection, Mapping)
            or set(projection)
            != {"schema", "userId", "generation", "bundleHash", "schedules"}
            or projection.get("schema") != _PORTABLE_PROJECTION_SCHEMA
            or projection.get("userId") != user_id
            or projection.get("generation") != generation
            or projection.get("bundleHash") != bundle_hash
            or not isinstance(projection.get("schedules"), list)
            or len(projection["schedules"]) > _MAX_SCHEDULES
        ):
            raise RuntimeError("portable schedule projection is invalid")
        result: list[dict[str, str]] = []
        prior_id: str | None = None
        for row in projection["schedules"]:
            if (
                not isinstance(row, Mapping)
                or set(row) != {"scheduleId", "userId", "taskType", "state"}
                or not isinstance(row.get("scheduleId"), str)
                or _SCHEDULE.fullmatch(row["scheduleId"]) is None
                or row.get("userId") != user_id
                or row.get("taskType")
                not in {"REMINDER", "READ_ONLY_AGENT_TURN"}
                or row.get("state") != "DISABLED"
                or (prior_id is not None and row["scheduleId"] <= prior_id)
            ):
                raise RuntimeError("portable schedule projection is invalid")
            prior_id = row["scheduleId"]
            result.append(dict(row))
        return result


class DynamoScheduleDefinitionReader:
    """Strong-read exact native definitions without delivery/provider authority."""

    def __init__(self, *, client: Any, table_name: str) -> None:
        if any(
            not callable(getattr(client, method, None))
            for method in ("query", "get_item")
        ):
            raise TypeError("schedule definition reader requires exact Dynamo methods")
        if not isinstance(table_name, str) or _TABLE.fullmatch(table_name) is None:
            raise ValueError("schedule definition table name is invalid")
        self._client = client
        self._table_name = table_name

    @staticmethod
    def _user(user_id: str) -> str:
        if not isinstance(user_id, str) or _USER.fullmatch(user_id) is None:
            raise ValueError("schedule capability user is invalid")
        return user_id

    def read_schedule(self, schedule_id: str) -> ScheduleSpecV1 | None:
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
        if not isinstance(response, Mapping):
            raise RuntimeError("scheduler control strong read is invalid")
        raw = response.get("Item")
        if raw is None:
            return None
        required_fields = {
            "PK",
            "SK",
            "userId",
            "scheduleUserId",
            "scheduleSortKey",
            "recordJson",
            "deliveryJson",
        }
        if not isinstance(raw, Mapping) or frozenset(raw) not in {
            frozenset(required_fields),
            frozenset({*required_fields, "ttl"}),
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
        ttl = raw.get("ttl")
        if spec.state == "ENABLED":
            if ttl is not None:
                raise RuntimeError("scheduler control record is invalid")
        elif (
            not isinstance(ttl, Mapping)
            or set(ttl) != {"N"}
            or not isinstance(ttl.get("N"), str)
            or not ttl["N"].isdigit()
            or int(ttl["N"]) <= 0
        ):
            raise RuntimeError("scheduler control record is invalid")
        return spec

    def definitions_for_user(self, user_id: str) -> list[dict[str, Any]]:
        user_id = self._user(user_id)
        definitions: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_cursors: set[tuple[str, str]] = set()
        start_key: dict[str, Any] | None = None
        while True:
            request = {
                "TableName": self._table_name,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"USER#{user_id}"},
                    ":prefix": {"S": "SCHEDULE#"},
                },
                "ProjectionExpression": "PK, SK, recordType, scheduleId",
                "ConsistentRead": True,
                "Limit": _MAX_SCHEDULES + 1,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._client.query(**request)
            if not isinstance(response, Mapping):
                raise RuntimeError("schedule listing is invalid")
            raw_items = response.get("Items")
            if not isinstance(raw_items, list) or len(raw_items) > _MAX_SCHEDULES + 1:
                raise RuntimeError("schedule listing page is invalid")
            for raw in raw_items:
                if not isinstance(raw, Mapping) or set(raw) != {
                    "PK",
                    "SK",
                    "recordType",
                    "scheduleId",
                }:
                    raise RuntimeError("schedule owner record is invalid")
                pk = _string_attribute(raw, "PK")
                sk = _string_attribute(raw, "SK")
                schedule_id = _string_attribute(raw, "scheduleId")
                if (
                    pk != f"USER#{user_id}"
                    or sk != f"SCHEDULE#{schedule_id}"
                    or _string_attribute(raw, "recordType") != "SCHEDULE_OWNER"
                    or _SCHEDULE.fullmatch(schedule_id) is None
                ):
                    raise RuntimeError("schedule owner crossed a tenant boundary")
                if schedule_id in seen:
                    raise RuntimeError("schedule owner inventory contains a duplicate")
                seen.add(schedule_id)
                spec = self.read_schedule(schedule_id)
                # Terminal STATE rows have bounded physical retention while
                # their owner rows deliberately remain for provider-orphan
                # discovery during account deletion. Missing STATE is thus an
                # inert historical owner, not an authority race.
                if spec is None:
                    continue
                if spec.user_id != user_id:
                    raise RuntimeError("schedule listing authority changed during read")
                definitions.append(spec.to_mapping())
                if len(definitions) > _MAX_SCHEDULES:
                    raise RuntimeError("live schedule listing exceeds its bound")

            cursor = response.get("LastEvaluatedKey")
            if cursor is None:
                break
            if not isinstance(cursor, Mapping) or set(cursor) != {"PK", "SK"}:
                raise RuntimeError("schedule listing cursor is invalid")
            cursor_pk = _string_attribute(cursor, "PK")
            cursor_sk = _string_attribute(cursor, "SK")
            marker = (cursor_pk, cursor_sk)
            if (
                cursor_pk != f"USER#{user_id}"
                or not cursor_sk.startswith("SCHEDULE#")
                or _SCHEDULE.fullmatch(cursor_sk.removeprefix("SCHEDULE#")) is None
                or marker in seen_cursors
            ):
                raise RuntimeError("schedule listing cursor is invalid")
            seen_cursors.add(marker)
            start_key = {
                "PK": {"S": cursor_pk},
                "SK": {"S": cursor_sk},
            }
        definitions.sort(key=lambda item: item["scheduleId"])
        return definitions


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
        imported_schedules: Any | None = None,
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
        if imported_schedules is not None and not any(
            callable(getattr(imported_schedules, method, None))
            for method in ("disabled_schedule_views", "disabled_schedules")
        ):
            raise TypeError("imported schedule projection is invalid")
        self._client = client
        self._table_name = table_name
        self._authority_table_name = authority_table_name
        self._catalog_digest = catalog_digest
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._definitions = DynamoScheduleDefinitionReader(
            client=client,
            table_name=table_name,
        )
        self._imported_schedules = imported_schedules

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

    def _delivery(
        self, user_id: str, invocation_id: str, now: int
    ) -> Mapping[str, str]:
        if (
            not isinstance(invocation_id, str)
            or _INVOCATION.fullmatch(invocation_id) is None
        ):
            raise ValueError("schedule proposal invocation is invalid")
        response = self._client.get_item(
            TableName=self._authority_table_name,
            Key={
                "PK": {"S": subject_partition_key(user_id)},
                "SK": {"S": f"DELIVERY#{invocation_id}"},
            },
            ConsistentRead=True,
        )
        raw = response.get("Item")
        if not isinstance(raw, Mapping) or set(raw) != {
            "PK",
            "SK",
            "ownerBinding",
            "recordJson",
            "ttl",
            "version",
        }:
            raise RuntimeError("schedule proposal delivery context is unavailable")
        if (
            _string_attribute(raw, "PK") != subject_partition_key(user_id)
            or _string_attribute(raw, "SK") != f"DELIVERY#{invocation_id}"
            or _string_attribute(raw, "ownerBinding")
            != derive_deletion_subject_binding(user_id)
            or raw.get("version") != {"N": "1"}
            or raw.get("ttl") is None
        ):
            raise RuntimeError("schedule proposal delivery context is invalid")
        ttl = raw["ttl"]
        if (
            not isinstance(ttl, Mapping)
            or set(ttl) != {"N"}
            or not isinstance(ttl.get("N"), str)
            or not ttl["N"].isdigit()
            or int(ttl["N"]) <= now
        ):
            raise RuntimeError("schedule proposal delivery context is expired")
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
        return self._definitions.read_schedule(schedule_id)

    def list_view(self, user_id: str) -> Mapping[str, Any]:
        user_id = self._user(user_id)
        native = self._definitions.definitions_for_user(user_id)
        schedules = [
            {
                "scheduleId": definition["scheduleId"],
                "taskType": definition["taskType"],
                "state": definition["state"],
                "nextRunAt": definition["nextRunAt"],
            }
            for definition in native
        ]
        seen = {item["scheduleId"] for item in schedules}
        if self._imported_schedules is not None:
            view_reader = getattr(
                self._imported_schedules,
                "disabled_schedule_views",
                None,
            )
            if callable(view_reader):
                imported_views = view_reader(user_id)
                if (
                    not isinstance(imported_views, list)
                    or len(imported_views) > _MAX_SCHEDULES
                ):
                    raise RuntimeError("imported schedule listing is invalid")
                for raw in imported_views:
                    if (
                        not isinstance(raw, Mapping)
                        or set(raw)
                        != {"scheduleId", "userId", "taskType", "state"}
                        or raw.get("userId") != user_id
                        or raw.get("state") != "DISABLED"
                        or not isinstance(raw.get("scheduleId"), str)
                        or _SCHEDULE.fullmatch(raw["scheduleId"]) is None
                        or raw.get("taskType")
                        not in {"REMINDER", "READ_ONLY_AGENT_TURN"}
                        or raw["scheduleId"] in seen
                    ):
                        raise RuntimeError("imported schedule listing is invalid")
                    seen.add(raw["scheduleId"])
                    schedules.append(
                        {
                            "scheduleId": raw["scheduleId"],
                            "taskType": raw["taskType"],
                            "state": "PAUSED",
                            "nextRunAt": None,
                        }
                    )
                imported = None
            else:
                imported = self._imported_schedules.disabled_schedules(user_id)
            if imported is not None and (
                not isinstance(imported, list) or len(imported) > _MAX_SCHEDULES
            ):
                raise RuntimeError("imported schedule listing is invalid")
            expected = set(ScheduleSpecV1.FIELDS) - {"schema", "nextRunAt"}
            for raw in imported or []:
                if not isinstance(raw, Mapping):
                    raise RuntimeError("imported schedule listing is invalid")
                # Portable-v2 historically admitted other inert schedule
                # descriptors. They remain portable, but only the exact
                # governed portable projection enters schedule.list.
                if set(raw) != expected:
                    if {
                        "scheduleId",
                        "userId",
                        "taskType",
                        "definition",
                        "definitionHash",
                        "revision",
                    }.issubset(raw):
                        raise RuntimeError("imported schedule listing is invalid")
                    continue
                if (
                    raw.get("userId") != user_id
                    or raw.get("state") != "DISABLED"
                ):
                    raise RuntimeError("imported schedule listing is invalid")
                candidate = dict(raw)
                candidate.update(
                    schema=ScheduleSpecV1.SCHEMA,
                    state="PAUSED",
                    nextRunAt=None,
                )
                try:
                    spec = ScheduleSpecV1.from_mapping(candidate)
                except (TypeError, ValueError):
                    raise RuntimeError("imported schedule listing is invalid") from None
                if spec.schedule_id in seen:
                    raise RuntimeError("imported schedule listing contains a duplicate")
                seen.add(spec.schedule_id)
                schedules.append(
                    {
                        "scheduleId": spec.schedule_id,
                        "taskType": spec.task_type,
                        "state": "PAUSED",
                        "nextRunAt": None,
                    }
                )
        if len(schedules) > _MAX_SCHEDULES:
            raise RuntimeError("schedule listing exceeds its bound")
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
        now = self._now()
        delivery = self._delivery(user_id, invocation_id, now)
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
        now = self._now()
        delivery = self._delivery(user_id, invocation_id, now)
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


__all__ = [
    "DynamoPortableScheduleProjectionReader",
    "DynamoScheduleCapabilityPort",
    "DynamoScheduleDefinitionReader",
]
