from __future__ import annotations

import pytest

from portable.manifest import ImportRejected, ImportUncertain
from portable.staging import DynamoStagedImportStore


USER = "user_founder"


class ConditionalError(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """Minimal conditional single-item table imitating DynamoDB CAS semantics."""

    def __init__(self, *, outage=False):
        self.items = {}
        self.outage = outage
        self.writes = 0

    @staticmethod
    def _pk(key):
        return (key["PK"], key["SK"])

    def get_item(self, *, Key, ConsistentRead=False):
        if self.outage:
            raise RuntimeError("dynamo unavailable")
        item = self.items.get(self._pk(Key))
        return {"Item": item} if item is not None else {}

    def update_item(self, *, Key, ExpressionAttributeValues, **_kwargs):
        self.writes += 1
        if self.outage:
            raise RuntimeError("dynamo unavailable")
        pk = self._pk(Key)
        current = self.items.get(pk)
        expected = ExpressionAttributeValues[":expected"]
        current_generation = current["generation"] if current else 0
        if current_generation != expected:
            raise ConditionalError()
        self.items[pk] = {
            "PK": Key["PK"],
            "SK": Key["SK"],
            "recordType": ExpressionAttributeValues[":recordType"],
            "userId": ExpressionAttributeValues[":userId"],
            "staged": ExpressionAttributeValues[":staged"],
            "generation": ExpressionAttributeValues[":nextGeneration"],
        }
        return {"Attributes": self.items[pk]}


def _staged():
    return {"format": "personal-operator.portable.v2", "records": {}, "workspace": {}}


def test_first_activation_advances_from_zero():
    store = DynamoStagedImportStore(FakeTable())
    assert store.load_generation(USER) == 0
    generation = store.swap(USER, expected_generation=0, staged=_staged())
    assert generation == 1
    assert store.load_generation(USER) == 1


def test_stale_generation_is_rejected_without_partial_write():
    table = FakeTable()
    store = DynamoStagedImportStore(table)
    store.swap(USER, expected_generation=0, staged=_staged())
    with pytest.raises(ImportRejected):
        store.swap(USER, expected_generation=0, staged=_staged())
    # The generation only advanced once; the losing swap wrote nothing.
    assert store.load_generation(USER) == 1


def test_outage_is_uncertain_not_a_silent_success():
    store = DynamoStagedImportStore(FakeTable(outage=True))
    with pytest.raises(ImportUncertain):
        store.swap(USER, expected_generation=0, staged=_staged())


def test_oversized_staged_payload_is_rejected():
    store = DynamoStagedImportStore(FakeTable())
    with pytest.raises(ImportRejected):
        store.swap(
            USER,
            expected_generation=0,
            staged={"blob": "x" * (8 * 1024 * 1024 + 1)},
        )
