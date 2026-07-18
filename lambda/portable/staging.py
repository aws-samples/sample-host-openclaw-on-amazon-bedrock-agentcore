"""Durable staged-generation store for atomic portable-import activation.

Activation lands through a single conditional generation compare-and-swap,
mirroring the retention sweep cursor CAS.  Either the swap commits the whole
staged bundle and advances the generation, or it fails and no partial state is
written.  The store never reads or writes any live-authority partition.
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Mapping

from .manifest import (
    ImportRejected,
    ImportUncertain,
    canonical_json,
    user_id as _user_id,
)


_RECORD_TYPE = "PORTABLE_STAGED_IMPORT_V2"
_MAX_STAGED_BYTES = 8 * 1024 * 1024


def _staged_key(user_id: str) -> dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": "PORTABLE#STAGED_IMPORT"}


def _generation(value: object) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ImportUncertain("staged import generation is invalid")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ImportUncertain("staged import generation is invalid")
    return value


def _conditional_failure(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


class DynamoStagedImportStore:
    def __init__(self, table) -> None:
        if table is None:
            raise ValueError("staged import table is required")
        self._table = table

    def load_generation(self, user_id: str) -> int:
        user_id = _user_id(user_id)
        try:
            response = self._table.get_item(
                Key=_staged_key(user_id), ConsistentRead=True
            )
        except Exception as error:
            raise ImportUncertain("staged import read failed") from error
        if not isinstance(response, Mapping):
            raise ImportUncertain("staged import read returned invalid data")
        item = response.get("Item")
        if item is None:
            return 0
        if not isinstance(item, Mapping) or item.get("recordType") != _RECORD_TYPE:
            raise ImportUncertain("staged import record is invalid")
        return _generation(item.get("generation"))

    def swap(self, user_id: str, *, expected_generation: int, staged) -> int:
        user_id = _user_id(user_id)
        expected = _generation(expected_generation)
        serialized = canonical_json(staged)
        if len(serialized) > _MAX_STAGED_BYTES:
            raise ImportRejected("staged import bundle exceeds its size limit")
        next_generation = expected + 1
        try:
            self._table.update_item(
                Key=_staged_key(user_id),
                UpdateExpression=(
                    "SET #recordType=:recordType, #staged=:staged, "
                    "#userId=:userId, #generation=:nextGeneration"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(#generation) AND "
                    "attribute_not_exists(#recordType) AND :expected=:zero) OR "
                    "(#recordType=:recordType AND #generation=:expected)"
                ),
                ExpressionAttributeNames={
                    "#recordType": "recordType",
                    "#staged": "staged",
                    "#userId": "userId",
                    "#generation": "generation",
                },
                ExpressionAttributeValues={
                    ":recordType": _RECORD_TYPE,
                    ":staged": json.loads(serialized.decode("utf-8")),
                    ":userId": user_id,
                    ":nextGeneration": next_generation,
                    ":expected": expected,
                    ":zero": 0,
                },
            )
        except Exception as error:
            if _conditional_failure(error):
                raise ImportRejected(
                    "staged import generation changed before activation"
                ) from error
            raise ImportUncertain("staged import write failed") from error
        return next_generation
