"""Bounded, pseudonymous pilot scan state and one-bit usefulness feedback."""

from __future__ import annotations

import base64
from decimal import Decimal
import hashlib
import hmac
import os
import re
import time
from typing import Mapping


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_SCAN_ID = re.compile(
    r"scan_(?P<started>[0-9]{20})_(?P<random>[A-Za-z0-9_-]{32})"
)
_PK = re.compile(r"SCANUSER#[0-9a-f]{64}")
_FAILURE_CODES = frozenset(
    {"AUTHORIZATION", "PROVIDER_UNAVAILABLE", "RANKING", "INTERNAL"}
)
_FEEDBACK = frozenset({"USEFUL", "NOT_USEFUL"})
_TERMINAL_SUCCESS = frozenset({"SUCCEEDED", "EMPTY"})
_RECORD_TYPE = "PILOT_SCAN_MEASUREMENT_V1"
_RETENTION_SECONDS = 30 * 24 * 60 * 60


class ScanMeasurementError(RuntimeError):
    pass


class ScanFeedbackConflict(ScanMeasurementError):
    pass


def _conditional_failure(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and isinstance(response.get("Error"), Mapping)
        and response["Error"].get("Code") == "ConditionalCheckFailedException"
    )


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def _now(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScanMeasurementError("scan measurement clock is invalid")
    return value


def _integer(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ScanMeasurementError("scan measurement is invalid")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScanMeasurementError("scan measurement is invalid")
    if (allow_zero and value < 0) or (not allow_zero and value <= 0):
        raise ScanMeasurementError("scan measurement is invalid")
    return value


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DynamoScanMeasurements:
    """Store no raw user/source identity and no model or provider content."""

    def __init__(
        self,
        table,
        *,
        identity_key: bytes,
        now=None,
        random_bytes=None,
    ) -> None:
        if (
            not callable(getattr(table, "put_item", None))
            or not callable(getattr(table, "update_item", None))
            or not callable(getattr(table, "query", None))
            or not callable(getattr(table, "get_item", None))
            or not callable(getattr(table, "delete_item", None))
        ):
            raise TypeError("scan measurement table is invalid")
        if not isinstance(identity_key, bytes) or len(identity_key) < 32:
            raise ValueError("scan measurement identity key is invalid")
        self._table = table
        self._identity_key = identity_key
        self._now = now or (lambda: int(time.time()))
        self._random = random_bytes or os.urandom

    def _pk(self, user_id: str) -> str:
        user_id = _user_id(user_id)
        digest = hmac.new(
            self._identity_key,
            b"personal-operator-scan-user-v1\0" + user_id.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"SCANUSER#{digest}"

    @staticmethod
    def _scan_key(user_pk: str, scan_id: object) -> dict[str, str]:
        match = _SCAN_ID.fullmatch(scan_id) if isinstance(scan_id, str) else None
        if match is None or _PK.fullmatch(user_pk) is None:
            raise ValueError("scan identity is invalid")
        return {
            "PK": user_pk,
            "SK": f"SCAN#{match.group('started')}#{match.group('random')}",
        }

    @staticmethod
    def _projection(item: object) -> dict[str, object]:
        if not isinstance(item, Mapping):
            raise ScanMeasurementError("scan measurement is invalid")
        status = item.get("status")
        base = {
            "PK",
            "SK",
            "recordType",
            "scanId",
            "status",
            "startedAt",
            "ttl",
        }
        terminal = {"completedAt"}
        if status == "RUNNING":
            expected = base
        elif status in _TERMINAL_SUCCESS:
            expected = base | terminal | {"resultCount"}
        elif status == "FAILED":
            expected = base | terminal | {"failureCode"}
        else:
            raise ScanMeasurementError("scan measurement status is invalid")
        if "feedback" in item:
            expected = expected | {"feedback"}
        if set(item) != expected:
            raise ScanMeasurementError("scan measurement fields are invalid")
        pk = item.get("PK")
        sk = item.get("SK")
        scan_id = item.get("scanId")
        match = _SCAN_ID.fullmatch(scan_id) if isinstance(scan_id, str) else None
        if (
            not isinstance(pk, str)
            or _PK.fullmatch(pk) is None
            or match is None
            or sk != f"SCAN#{match.group('started')}#{match.group('random')}"
            or item.get("recordType") != _RECORD_TYPE
        ):
            raise ScanMeasurementError("scan measurement binding is invalid")
        started = _integer(item.get("startedAt"))
        ttl = _integer(item.get("ttl"))
        if started != int(match.group("started")) or ttl != started + _RETENTION_SECONDS:
            raise ScanMeasurementError("scan measurement retention is invalid")
        completed = None
        result_count = None
        failure = None
        feedback = item.get("feedback")
        if feedback is not None and (
            status not in _TERMINAL_SUCCESS or feedback not in _FEEDBACK
        ):
            raise ScanMeasurementError("scan feedback is invalid")
        if status != "RUNNING":
            completed = _integer(item.get("completedAt"))
            if completed < started:
                raise ScanMeasurementError("scan completion time is invalid")
        if status in _TERMINAL_SUCCESS:
            result_count = _integer(item.get("resultCount"), allow_zero=True)
            if result_count > 3 or (status == "EMPTY") != (result_count == 0):
                raise ScanMeasurementError("scan result count is invalid")
        elif status == "FAILED":
            failure = item.get("failureCode")
            if failure not in _FAILURE_CODES:
                raise ScanMeasurementError("scan failure code is invalid")
        return {
            "scanId": scan_id,
            "status": status,
            "startedAt": started,
            "completedAt": completed,
            "resultCount": result_count,
            "failureCode": failure,
            "feedback": feedback,
        }

    def _get(self, user_pk: str, scan_id: str) -> Mapping | None:
        response = self._table.get_item(
            Key=self._scan_key(user_pk, scan_id),
            ConsistentRead=True,
        )
        if not isinstance(response, Mapping):
            raise ScanMeasurementError("scan measurement read is invalid")
        item = response.get("Item")
        return item if isinstance(item, Mapping) else None

    def start(self, user_id: str) -> str:
        user_pk = self._pk(user_id)
        started = _now(self._now())
        random = self._random(24)
        if not isinstance(random, bytes) or len(random) != 24:
            raise ScanMeasurementError("scan randomness is invalid")
        suffix = _b64url(random)
        scan_id = f"scan_{started:020d}_{suffix}"
        key = self._scan_key(user_pk, scan_id)
        item = {
            **key,
            "recordType": _RECORD_TYPE,
            "scanId": scan_id,
            "status": "RUNNING",
            "startedAt": started,
            "ttl": started + _RETENTION_SECONDS,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except Exception as error:
            if _conditional_failure(error):
                raise ScanMeasurementError("scan identity collision") from None
            raise ScanMeasurementError("scan measurement start failed") from error
        return scan_id

    def latest(self, user_id: str) -> dict[str, object] | None:
        user_pk = self._pk(user_id)
        response = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": user_pk, ":prefix": "SCAN#"},
            ConsistentRead=True,
            ScanIndexForward=False,
            Limit=1,
        )
        items = response.get("Items") if isinstance(response, Mapping) else None
        if not isinstance(items, list) or len(items) > 1:
            raise ScanMeasurementError("latest scan read is invalid")
        return self._projection(items[0]) if items else None

    def complete(
        self,
        user_id: str,
        scan_id: str,
        *,
        result_count: int,
    ) -> dict[str, object]:
        user_pk = self._pk(user_id)
        if (
            isinstance(result_count, bool)
            or not isinstance(result_count, int)
            or not 0 <= result_count <= 3
        ):
            raise ValueError("scan result count is invalid")
        status = "EMPTY" if result_count == 0 else "SUCCEEDED"
        completed = _now(self._now())
        key = self._scan_key(user_pk, scan_id)
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #status=:status, completedAt=:completed, resultCount=:count"
                ),
                ConditionExpression=(
                    "recordType=:recordType AND scanId=:scanId AND "
                    "#status=:running AND attribute_not_exists(completedAt)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": status,
                    ":completed": completed,
                    ":count": result_count,
                    ":recordType": _RECORD_TYPE,
                    ":scanId": scan_id,
                    ":running": "RUNNING",
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            current = self._get(user_pk, scan_id)
            if current is not None:
                projection = self._projection(current)
                if (
                    projection["status"] == status
                    and projection["resultCount"] == result_count
                ):
                    return projection
            raise ScanMeasurementError("scan completion is unavailable") from error
        attributes = response.get("Attributes") if isinstance(response, Mapping) else None
        return self._projection(attributes)

    def fail(
        self,
        user_id: str,
        scan_id: str,
        *,
        failure_code: str,
    ) -> dict[str, object]:
        user_pk = self._pk(user_id)
        if failure_code not in _FAILURE_CODES:
            raise ValueError("scan failure code is invalid")
        completed = _now(self._now())
        key = self._scan_key(user_pk, scan_id)
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #status=:failed, completedAt=:completed, "
                    "failureCode=:failure"
                ),
                ConditionExpression=(
                    "recordType=:recordType AND scanId=:scanId AND "
                    "#status=:running AND attribute_not_exists(completedAt)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":completed": completed,
                    ":failure": failure_code,
                    ":recordType": _RECORD_TYPE,
                    ":scanId": scan_id,
                    ":running": "RUNNING",
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            current = self._get(user_pk, scan_id)
            if current is not None:
                projection = self._projection(current)
                if (
                    projection["status"] == "FAILED"
                    and projection["failureCode"] == failure_code
                ):
                    return projection
            raise ScanMeasurementError("scan failure state is unavailable") from error
        attributes = response.get("Attributes") if isinstance(response, Mapping) else None
        return self._projection(attributes)

    def feedback(
        self,
        user_id: str,
        scan_id: str,
        *,
        response: str,
    ) -> dict[str, object]:
        user_pk = self._pk(user_id)
        if response not in _FEEDBACK:
            raise ValueError("scan feedback is invalid")
        key = self._scan_key(user_pk, scan_id)
        try:
            result = self._table.update_item(
                Key=key,
                UpdateExpression="SET feedback=:feedback",
                ConditionExpression=(
                    "recordType=:recordType AND scanId=:scanId AND "
                    "#status IN (:succeeded,:empty) AND "
                    "attribute_not_exists(feedback)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":feedback": response,
                    ":recordType": _RECORD_TYPE,
                    ":scanId": scan_id,
                    ":succeeded": "SUCCEEDED",
                    ":empty": "EMPTY",
                },
                ReturnValues="ALL_NEW",
            )
        except Exception as error:
            current = self._get(user_pk, scan_id)
            if current is None:
                raise ScanMeasurementError("scan feedback is unavailable") from error
            projection = self._projection(current)
            if projection["feedback"] == response:
                return projection
            if projection["feedback"] in _FEEDBACK:
                raise ScanFeedbackConflict(
                    "scan feedback was already recorded"
                ) from None
            raise ScanMeasurementError("scan feedback is unavailable") from error
        attributes = result.get("Attributes") if isinstance(result, Mapping) else None
        return self._projection(attributes)

    def delete_user_records(self, user_id: str) -> None:
        """Boundedly purge the user's pseudonymous scan partition."""

        user_pk = self._pk(user_id)
        start_key = None
        keys: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for _page in range(40):
            request = {
                "KeyConditionExpression": (
                    "PK = :pk AND begins_with(SK, :prefix)"
                ),
                "ExpressionAttributeValues": {
                    ":pk": user_pk,
                    ":prefix": "SCAN#",
                },
                "ProjectionExpression": "PK, SK",
                "ConsistentRead": True,
                "Limit": 100,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.query(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list) or len(items) > 100:
                raise ScanMeasurementError("scan deletion listing is invalid")
            for item in items:
                if not isinstance(item, Mapping):
                    raise ScanMeasurementError("scan deletion listing is invalid")
                pk = item.get("PK")
                sk = item.get("SK")
                signature = (pk, sk)
                if (
                    pk != user_pk
                    or not isinstance(sk, str)
                    or re.fullmatch(
                        r"SCAN#[0-9]{20}#[A-Za-z0-9_-]{32}", sk
                    )
                    is None
                    or signature in seen
                ):
                    raise ScanMeasurementError("scan deletion listing is invalid")
                seen.add(signature)
                keys.append({"PK": pk, "SK": sk})
            next_key = response.get("LastEvaluatedKey")
            if next_key is None:
                break
            if (
                not isinstance(next_key, Mapping)
                or set(next_key) != {"PK", "SK"}
                or next_key.get("PK") != user_pk
                or not isinstance(next_key.get("SK"), str)
            ):
                raise ScanMeasurementError("scan deletion cursor is invalid")
            start_key = dict(next_key)
        else:
            raise ScanMeasurementError("scan deletion exceeded its page bound")
        for key in keys:
            try:
                self._table.delete_item(Key=key)
            except Exception:
                # Reconcile the complete partition below. A response can be
                # lost after DynamoDB durably applies the exact delete.
                pass
        remaining = self._table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": user_pk, ":prefix": "SCAN#"},
            ProjectionExpression="PK, SK",
            ConsistentRead=True,
            Limit=1,
        )
        items = remaining.get("Items") if isinstance(remaining, Mapping) else None
        if not isinstance(items, list) or items:
            raise ScanMeasurementError("scan deletion outcome is uncertain")
