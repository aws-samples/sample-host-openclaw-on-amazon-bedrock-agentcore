"""Typed inert record projections for portable-state v2.

Portable records are deliberately list/history views.  They never implement
authority reads, schedule fire, connector dispatch, action reconciliation, or
compute deduplication.  Native active records are normalized before export;
imported rows are validated as inert and rebound to the authenticated target.
"""

from __future__ import annotations

import re
from typing import Mapping

from actions.models import EffectReceipt
from capabilities.contracts import (
    CapabilityInstallationV1,
    ComputeReceiptV1,
    ContractValidationError,
    EffectReceiptV1,
)

from .manifest import ImportRejected, RECORD_CATEGORIES, scan_for_secrets, user_id


_CONNECTOR_ID = re.compile(r"[a-z][a-z0-9.-]{1,127}")
_ARMED_SCHEDULE_FIELDS = frozenset(
    {
        "deliveryTarget",
        "enabled",
        "nextRun",
        "nextRunAt",
        "connectionRef",
        "connectionRefs",
    }
)


def _record_rows(value: object) -> dict[str, list[dict]]:
    if not isinstance(value, Mapping) or set(value) != RECORD_CATEGORIES:
        raise ImportRejected("portable record categories are invalid")
    result: dict[str, list[dict]] = {}
    for category in RECORD_CATEGORIES:
        rows = value.get(category)
        if not isinstance(rows, list) or any(
            not isinstance(row, Mapping) for row in rows
        ):
            raise ImportRejected(
                "portable live state requires structured record rows"
            )
        result[category] = [dict(row) for row in rows]
    return result


def _installation(row: Mapping, *, target_user_id: str) -> dict:
    try:
        parsed = CapabilityInstallationV1.from_mapping(row).to_mapping()
    except (ContractValidationError, TypeError, ValueError) as error:
        raise ImportRejected("portable installation metadata is invalid") from error
    parsed.update(
        userId=target_user_id,
        state="PAUSED",
        connectionRefs=[],
        killSwitch=True,
    )
    return CapabilityInstallationV1.from_mapping(parsed).to_mapping()


def _connector(row: Mapping, *, normalize: bool) -> dict:
    if (
        set(row) != {"connectorId", "state"}
        or not isinstance(row.get("connectorId"), str)
        or _CONNECTOR_ID.fullmatch(row["connectorId"]) is None
        or not isinstance(row.get("state"), str)
    ):
        raise ImportRejected("portable connector descriptor is invalid")
    if not normalize and row["state"] != "DISCONNECTED":
        raise ImportRejected("portable connector descriptor is not disconnected")
    return {"connectorId": row["connectorId"], "state": "DISCONNECTED"}


def _compute_receipt(row: Mapping) -> dict:
    try:
        return ComputeReceiptV1.from_mapping(row).to_mapping()
    except (ContractValidationError, TypeError, ValueError) as error:
        raise ImportRejected("portable compute receipt is invalid") from error


def _effect_receipt(row: Mapping) -> dict:
    try:
        return EffectReceipt.from_record(row).record()
    except (TypeError, ValueError):
        try:
            return EffectReceiptV1.from_mapping(row).to_mapping()
        except (ContractValidationError, TypeError, ValueError) as error:
            raise ImportRejected("portable effect receipt is invalid") from error


def _schedule(row: Mapping, *, target_user_id: str, normalize: bool) -> dict:
    if not normalize and (
        row.get("state") != "DISABLED"
        or row.get("userId") != target_user_id
        or any(field in row for field in _ARMED_SCHEDULE_FIELDS)
    ):
        raise ImportRejected("portable schedule is not disabled")
    landed = {
        key: value for key, value in row.items() if key not in _ARMED_SCHEDULE_FIELDS
    }
    landed["userId"] = target_user_id
    landed["state"] = "DISABLED"
    return landed


def normalize_export_records(value: object, *, owner_id: str) -> dict[str, list[dict]]:
    """Normalize native active records into the authority-free wire shape."""

    owner = user_id(owner_id)
    records = _record_rows(value)
    result = {category: [] for category in RECORD_CATEGORIES}
    for row in records["memory"]:
        scan_for_secrets(row)
        memory = dict(row)
        if "userId" in memory:
            memory["userId"] = owner
        result["memory"].append(memory)
    for row in records["schedules"]:
        scan_for_secrets(row)
        result["schedules"].append(
            _schedule(row, target_user_id=owner, normalize=True)
        )
    for row in records["installed_packs"]:
        scan_for_secrets(row)
        result["installed_packs"].append(
            _installation(row, target_user_id=owner)
        )
    for row in records["connectors"]:
        scan_for_secrets(row)
        result["connectors"].append(_connector(row, normalize=True))
    for row in records["compute_receipts"]:
        scan_for_secrets(row)
        result["compute_receipts"].append(_compute_receipt(row))
    for row in records["receipts"]:
        scan_for_secrets(row)
        result["receipts"].append(_effect_receipt(row))
    return result


def validate_bundle_records(value: object) -> dict[str, list[dict]]:
    """Validate that every serialized record is already inert and typed."""

    records = _record_rows(value)
    result = {category: [] for category in RECORD_CATEGORIES}
    for category, rows in records.items():
        for row in rows:
            scan_for_secrets(row)
            record_type = row.get("recordType")
            if isinstance(record_type, str) and record_type.upper() in {
                "USER_TOMBSTONE",
                "CHANNEL_TOMBSTONE",
                "TOMBSTONE",
            }:
                raise ImportRejected("portable bundle carries a deletion tombstone")
            normalized_keys = {
                re.sub(r"[^a-z0-9]", "", key.casefold())
                for key in row
                if isinstance(key, str)
            }
            if "deletionstatus" in normalized_keys:
                raise ImportRejected("portable bundle carries a deletion tombstone")
            state = row.get("state")
            if isinstance(state, str) and state.upper() in {
                "APPROVAL_PENDING",
                "PENDING",
                "SENDING",
                "DISPATCHING",
                "IN_FLIGHT",
                "QUEUED",
                "UNCERTAIN",
                "RECONCILING",
                "CONNECTED",
                "CONNECTING",
                "DISCONNECTING",
            }:
                raise ImportRejected("portable bundle carries live authority or effect")
            if "connectionenvelope" in normalized_keys:
                raise ImportRejected("portable bundle carries live authority")

        if category == "memory":
            result[category] = [dict(row) for row in rows]
        elif category == "schedules":
            result[category] = [
                _schedule(
                    row,
                    target_user_id=user_id(row.get("userId")),
                    normalize=False,
                )
                for row in rows
            ]
        elif category == "installed_packs":
            result[category] = []
            for row in rows:
                target = user_id(row.get("userId"))
                normalized = _installation(row, target_user_id=target)
                if normalized != row:
                    raise ImportRejected("portable installation metadata is not inert")
                result[category].append(normalized)
        elif category == "connectors":
            result[category] = [
                _connector(row, normalize=False) for row in rows
            ]
        elif category == "compute_receipts":
            result[category] = [_compute_receipt(row) for row in rows]
        elif category == "receipts":
            result[category] = [_effect_receipt(row) for row in rows]
    return result


def retarget_records(value: object, *, target_user_id: str) -> dict[str, list[dict]]:
    """Rebind user-bearing inert views without restoring native authority."""

    target = user_id(target_user_id)
    records = validate_bundle_records(value)
    result = {category: [dict(row) for row in rows] for category, rows in records.items()}
    for row in result["memory"]:
        if "userId" in row:
            row["userId"] = target
    result["schedules"] = [
        _schedule(row, target_user_id=target, normalize=True)
        for row in result["schedules"]
    ]
    result["installed_packs"] = [
        _installation(row, target_user_id=target)
        for row in result["installed_packs"]
    ]
    return result


__all__ = [
    "normalize_export_records",
    "retarget_records",
    "validate_bundle_records",
]
