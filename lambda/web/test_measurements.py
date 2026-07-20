from __future__ import annotations

from dataclasses import dataclass

import pytest

from .measurements import (
    DynamoScanMeasurements,
    ScanFeedbackConflict,
    ScanMeasurementError,
)


USER = "user_pilot"
OTHER = "user_other"


class ConditionalFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class Table:
    def __init__(self):
        self.items = {}
        self.calls = []

    def put_item(self, **kwargs):
        item = dict(kwargs["Item"])
        key = (item["PK"], item["SK"])
        self.calls.append(("put", kwargs))
        if key in self.items:
            raise ConditionalFailure()
        self.items[key] = item
        return {}

    def update_item(self, **kwargs):
        self.calls.append(("update", kwargs))
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        if item is None:
            raise ConditionalFailure()
        values = kwargs["ExpressionAttributeValues"]
        if ":count" in values:
            if item["status"] != "RUNNING":
                raise ConditionalFailure()
            item.update(
                status=values[":status"],
                completedAt=values[":completed"],
                resultCount=values[":count"],
            )
        elif ":failure" in values:
            if item["status"] != "RUNNING":
                raise ConditionalFailure()
            item.update(
                status="FAILED",
                completedAt=values[":completed"],
                failureCode=values[":failure"],
            )
        elif ":feedback" in values:
            if item["status"] not in {"SUCCEEDED", "EMPTY"} or "feedback" in item:
                raise ConditionalFailure()
            item["feedback"] = values[":feedback"]
        else:  # pragma: no cover - test adapter guard
            raise AssertionError(values)
        return {"Attributes": dict(item)}

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        prefix = kwargs["ExpressionAttributeValues"][":prefix"]
        values = [
            dict(item)
            for (item_pk, sk), item in self.items.items()
            if item_pk == pk and sk.startswith(prefix)
        ]
        values.sort(key=lambda item: item["SK"], reverse=True)
        return {"Items": values[: kwargs.get("Limit", len(values))]}

    def get_item(self, **kwargs):
        self.calls.append(("get", kwargs))
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, **kwargs):
        self.calls.append(("delete", kwargs))
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        self.items.pop(key, None)
        return {}


@dataclass
class Clock:
    value: int = 1_700_000_000

    def __call__(self):
        return self.value


class Random:
    def __init__(self):
        self.value = 0

    def __call__(self, size):
        self.value += 1
        return bytes([self.value]) * size


def measurements(table=None, clock=None):
    return DynamoScanMeasurements(
        table or Table(),
        identity_key=b"m" * 32,
        now=clock or Clock(),
        random_bytes=Random(),
    )


def test_scan_state_is_typed_bounded_and_contains_no_identity_or_source_data():
    table = Table()
    clock = Clock()
    store = measurements(table, clock)

    scan_id = store.start(USER)
    assert store.latest(USER) == {
        "scanId": scan_id,
        "status": "RUNNING",
        "startedAt": clock.value,
        "completedAt": None,
        "resultCount": None,
        "failureCode": None,
        "feedback": None,
    }

    clock.value += 12
    assert store.complete(USER, scan_id, result_count=2) == {
        "scanId": scan_id,
        "status": "SUCCEEDED",
        "startedAt": 1_700_000_000,
        "completedAt": clock.value,
        "resultCount": 2,
        "failureCode": None,
        "feedback": None,
    }
    item = next(iter(table.items.values()))
    assert USER not in repr(item)
    assert "subject" not in repr(item).casefold()
    assert "source" not in repr(item).casefold()
    assert set(item) == {
        "PK",
        "SK",
        "recordType",
        "scanId",
        "status",
        "startedAt",
        "completedAt",
        "resultCount",
        "ttl",
    }
    assert item["ttl"] == 1_700_000_000 + 30 * 24 * 60 * 60


def test_empty_and_failed_are_distinct_bounded_terminal_states():
    table = Table()
    clock = Clock()
    store = measurements(table, clock)

    empty = store.start(USER)
    clock.value += 1
    assert store.complete(USER, empty, result_count=0)["status"] == "EMPTY"

    failed = store.start(USER)
    clock.value += 2
    projection = store.fail(USER, failed, failure_code="AUTHORIZATION")
    assert projection["status"] == "FAILED"
    assert projection["failureCode"] == "AUTHORIZATION"
    assert projection["resultCount"] is None
    with pytest.raises(ValueError):
        store.fail(USER, store.start(USER), failure_code="provider said token abc")


def test_feedback_is_one_privacy_safe_response_per_successful_scan():
    table = Table()
    store = measurements(table)
    scan_id = store.start(USER)
    store.complete(USER, scan_id, result_count=1)

    first = store.feedback(USER, scan_id, response="USEFUL")
    assert first["feedback"] == "USEFUL"
    assert store.feedback(USER, scan_id, response="USEFUL") == first
    with pytest.raises(ScanFeedbackConflict):
        store.feedback(USER, scan_id, response="NOT_USEFUL")
    with pytest.raises(ValueError):
        store.feedback(USER, scan_id, response="Ada was useful")


def test_scan_identity_binding_prevents_cross_user_feedback_or_read():
    table = Table()
    store = measurements(table)
    scan_id = store.start(USER)
    store.complete(USER, scan_id, result_count=1)

    assert store.latest(OTHER) is None
    with pytest.raises(ScanMeasurementError, match="unavailable"):
        store.feedback(OTHER, scan_id, response="USEFUL")


def test_account_deletion_removes_only_the_pseudonymous_user_partition():
    table = Table()
    store = measurements(table)
    first = store.start(USER)
    store.complete(USER, first, result_count=1)
    second = store.start(USER)
    store.complete(USER, second, result_count=0)
    other = store.start(OTHER)
    store.complete(OTHER, other, result_count=1)

    store.delete_user_records(USER)

    assert store.latest(USER) is None
    assert store.latest(OTHER)["scanId"] == other
    deleted = [call for call in table.calls if call[0] == "delete"]
    assert len(deleted) == 2
    assert USER not in repr(deleted)


def test_account_deletion_makes_bounded_progress_above_four_thousand_records():
    class PagingTable(Table):
        def query(self, **kwargs):
            self.calls.append(("query", kwargs))
            pk = kwargs["ExpressionAttributeValues"][":pk"]
            prefix = kwargs["ExpressionAttributeValues"][":prefix"]
            values = sorted(
                (
                    dict(item)
                    for (item_pk, sk), item in self.items.items()
                    if item_pk == pk and sk.startswith(prefix)
                ),
                key=lambda item: item["SK"],
            )
            start = kwargs.get("ExclusiveStartKey")
            if start is not None:
                values = [item for item in values if item["SK"] > start["SK"]]
            limit = kwargs.get("Limit", len(values))
            page = values[:limit]
            response = {"Items": page}
            if len(values) > limit:
                response["LastEvaluatedKey"] = {
                    "PK": page[-1]["PK"],
                    "SK": page[-1]["SK"],
                }
            return response

    table = PagingTable()
    store = measurements(table)
    user_pk = store._pk(USER)
    other_pk = store._pk(OTHER)
    for index in range(4_001):
        key = {
            "PK": user_pk,
            "SK": f"SCAN#{1_700_000_000 + index:020d}#{index:032d}",
        }
        table.items[(key["PK"], key["SK"])] = key
    other_key = {
        "PK": other_pk,
        "SK": f"SCAN#{1_700_000_000:020d}#{'z' * 32}",
    }
    table.items[(other_key["PK"], other_key["SK"])] = other_key

    with pytest.raises(ScanMeasurementError, match="page bound"):
        store.delete_user_records(USER)

    assert sum(1 for pk, _ in table.items if pk == user_pk) == 1
    assert len([call for call in table.calls if call[0] == "delete"]) == 4_000
    assert (other_key["PK"], other_key["SK"]) in table.items

    store.delete_user_records(USER)

    assert all(pk != user_pk for pk, _ in table.items)
    assert (other_key["PK"], other_key["SK"]) in table.items
