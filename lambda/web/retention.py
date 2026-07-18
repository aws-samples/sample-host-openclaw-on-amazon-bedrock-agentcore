"""Logical retention, bounded export, and fail-safe account deletion."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import io
import json
from pathlib import PurePosixPath
import re
import time
from typing import Mapping
import zipfile


_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ACTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_TERMINAL_ACTIONS = frozenset(
    {
        "CONFIRMED",
        "REJECTED",
        "EXPIRED",
        "STALE",
        "CANCELLED",
        # An uncertain effect is permanently no-resend and only eligible for
        # observation-based reconciliation during its 90-day audit window.
        "UNCERTAIN",
    }
)
_EXPORT_CATEGORIES = frozenset({"memory", "schedules", "receipts"})
_SWEEP_CURSOR_FIELDS = {
    "expiry": ("PK", "SK"),
    "deletion-intents": ("PK", "SK"),
    "runtime-candidates": ("userId",),
}
_SWEEP_CURSOR_RECORD_TYPE = "RETENTION_SWEEP_CURSOR_V1"
_SWEEP_CURSOR_PK = "SYSTEM#RETENTION_SWEEP"


class DeletionPending(RuntimeError):
    pass


class ExportBoundaryError(ValueError):
    pass


def _user_id(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def assert_logically_live(record: object, *, now: int) -> Mapping:
    if not isinstance(record, Mapping):
        raise ValueError("record is invalid")
    ttl = record.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        raise ValueError("record TTL is invalid")
    if ttl <= now:
        raise ValueError("record is expired")
    return record


def _conditional_failure(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _cursor_generation(value: object) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise RuntimeError("retention sweep cursor generation is invalid")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("retention sweep cursor generation is invalid")
    return value


def _sweep_cursor(
    name: object, value: object
) -> dict[str, str] | None:
    fields = _SWEEP_CURSOR_FIELDS.get(name) if isinstance(name, str) else None
    if fields is None:
        raise RuntimeError("retention sweep cursor name is invalid")
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != set(fields)
        or any(
            not isinstance(value.get(field), str)
            or not value[field]
            or len(value[field]) > 512
            or "\x00" in value[field]
            for field in fields
        )
        or (
            "userId" in fields
            and _USER_ID.fullmatch(value["userId"]) is None
        )
    ):
        raise RuntimeError("retention sweep cursor is invalid")
    return {field: value[field] for field in fields}


def _sweep_cursor_key(name: object) -> dict[str, str]:
    _sweep_cursor(name, None)
    return {
        "PK": _SWEEP_CURSOR_PK,
        "SK": f"CURSOR#{name.upper().replace('-', '_')}",
    }


@dataclass(frozen=True, slots=True)
class SweepCursorLease:
    name: str
    cursor: dict[str, str] | None
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cursor", _sweep_cursor(self.name, self.cursor))
        object.__setattr__(
            self,
            "generation",
            _cursor_generation(self.generation),
        )


class DynamoSweepCursorStore:
    """Conditional durable progress for bounded retention table scans."""

    def __init__(self, table) -> None:
        if table is None:
            raise ValueError("retention sweep cursor table is required")
        self._table = table

    @staticmethod
    def _lease(name: str, item: object) -> SweepCursorLease:
        key = _sweep_cursor_key(name)
        if (
            not isinstance(item, Mapping)
            or set(item)
            != {"PK", "SK", "recordType", "cursor", "generation"}
            or item.get("PK") != key["PK"]
            or item.get("SK") != key["SK"]
            or item.get("recordType") != _SWEEP_CURSOR_RECORD_TYPE
        ):
            raise RuntimeError("retention sweep cursor record is invalid")
        return SweepCursorLease(
            name,
            item.get("cursor"),
            item.get("generation"),
        )

    def load(self, name: str) -> SweepCursorLease:
        key = _sweep_cursor_key(name)
        try:
            response = self._table.get_item(Key=key, ConsistentRead=True)
        except Exception as error:
            raise RuntimeError("retention sweep cursor read failed") from error
        if not isinstance(response, Mapping):
            raise RuntimeError("retention sweep cursor read returned invalid data")
        if "Item" not in response:
            return SweepCursorLease(name, None, 0)
        item = response.get("Item")
        if item is None:
            raise RuntimeError("retention sweep cursor read returned invalid data")
        return self._lease(name, item)

    def save(
        self,
        lease: SweepCursorLease,
        cursor: Mapping[str, str] | None,
    ) -> SweepCursorLease:
        if not isinstance(lease, SweepCursorLease):
            raise TypeError("retention cursor save requires a SweepCursorLease")
        next_cursor = _sweep_cursor(lease.name, cursor)
        next_generation = lease.generation + 1
        expected = SweepCursorLease(
            lease.name,
            next_cursor,
            next_generation,
        )
        key = _sweep_cursor_key(lease.name)
        try:
            response = self._table.update_item(
                Key=key,
                UpdateExpression=(
                    "SET #recordType=:recordType, #cursor=:cursor, "
                    "#generation=:nextGeneration"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(#generation) AND "
                    "attribute_not_exists(#recordType) AND "
                    ":expectedGeneration=:zero) OR "
                    "(#recordType=:recordType AND "
                    "#generation=:expectedGeneration)"
                ),
                ExpressionAttributeNames={
                    "#recordType": "recordType",
                    "#cursor": "cursor",
                    "#generation": "generation",
                },
                ExpressionAttributeValues={
                    ":recordType": _SWEEP_CURSOR_RECORD_TYPE,
                    ":cursor": next_cursor,
                    ":nextGeneration": next_generation,
                    ":expectedGeneration": lease.generation,
                    ":zero": 0,
                },
                ReturnValues="ALL_NEW",
            )
            attributes = (
                response.get("Attributes")
                if isinstance(response, Mapping)
                else None
            )
            if self._lease(lease.name, attributes) != expected:
                raise RuntimeError("retention sweep cursor write is invalid")
            return expected
        except Exception as error:
            try:
                current = self.load(lease.name)
            except Exception:
                current = None
            if current == expected:
                return expected
            raise RuntimeError("retention sweep cursor write failed") from error


class _VolatileSweepCursorStore:
    """In-process fallback for direct sweeper use outside composition."""

    def __init__(self) -> None:
        self._leases: dict[str, SweepCursorLease] = {}

    def load(self, name: str) -> SweepCursorLease:
        _sweep_cursor(name, None)
        return self._leases.get(name, SweepCursorLease(name, None, 0))

    def save(
        self,
        lease: SweepCursorLease,
        cursor: Mapping[str, str] | None,
    ) -> SweepCursorLease:
        if not isinstance(lease, SweepCursorLease):
            raise TypeError("retention cursor save requires a SweepCursorLease")
        current = self.load(lease.name)
        if current != lease:
            raise RuntimeError("retention sweep cursor write failed")
        saved = SweepCursorLease(
            lease.name,
            _sweep_cursor(lease.name, cursor),
            lease.generation + 1,
        )
        self._leases[lease.name] = saved
        return saved


class DynamoExpirySweeper:
    """Bounded physical cleanup for records already logically expired by TTL."""

    MAX_ITEMS = 1_000
    MAX_PAGES = 20

    def __init__(
        self,
        table,
        *,
        now=None,
        cursor_store=None,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= self.MAX_PAGES
        ):
            raise ValueError("retention sweep page bound is invalid")
        if cursor_store is None:
            cursor_store = _VolatileSweepCursorStore()
        if not callable(getattr(cursor_store, "load", None)) or not callable(
            getattr(cursor_store, "save", None)
        ):
            raise ValueError("retention sweep cursor store is invalid")
        self._table = table
        self._now = now or (lambda: int(time.time()))
        self._cursors = cursor_store
        self._max_pages = max_pages

    @staticmethod
    def _key(item: object, *, now: int) -> tuple[dict[str, str], int]:
        if not isinstance(item, Mapping):
            raise RuntimeError("expired record is not allowlisted")
        pk = item.get("PK")
        sk = item.get("SK")
        ttl = item.get("ttl")
        if (
            not isinstance(pk, str)
            or not isinstance(sk, str)
            or isinstance(ttl, bool)
            or not isinstance(ttl, int)
            or ttl > now
        ):
            raise RuntimeError("expired record is not allowlisted")
        digest_namespaces = {
            "CONNECT": "CONNECT",
            "SESSION": "SESSION",
            "OAUTHSTATE": "OAUTHSTATE",
            "OAUTH_STATE": "OAUTH_STATE",
        }
        allowed = False
        for prefix, expected_sk in digest_namespaces.items():
            marker = f"{prefix}#"
            if pk.startswith(marker):
                allowed = (
                    sk == expected_sk
                    and _DIGEST.fullmatch(pk[len(marker) :]) is not None
                )
                break
        if pk.startswith("SCANUSER#"):
            digest = pk[len("SCANUSER#") :]
            allowed = (
                _DIGEST.fullmatch(digest) is not None
                and re.fullmatch(
                    r"SCAN#[0-9]{20}#[A-Za-z0-9_-]{32}", sk
                )
                is not None
                and item.get("recordType") == "PILOT_SCAN_MEASUREMENT_V1"
                and item.get("userId") is None
            )
        if pk.startswith("USER#"):
            user_id = pk[5:]
            if _USER_ID.fullmatch(user_id) is None:
                allowed = False
            elif sk == "GMAIL#OPPORTUNITIES" or re.fullmatch(
                r"GMAIL#DRAFT#[A-Za-z0-9_-]{8,128}#[0-9]{10}", sk
            ):
                allowed = True
            elif sk.startswith("ACTION#"):
                action_id = sk[7:]
                allowed = (
                    _ACTION_ID.fullmatch(action_id) is not None
                    and item.get("state") in _TERMINAL_ACTIONS
                )
            elif sk.startswith("TELEGRAM_CALLBACK#"):
                digest = sk[len("TELEGRAM_CALLBACK#") :]
                allowed = (
                    _DIGEST.fullmatch(digest) is not None
                    and item.get("recordType") == "TELEGRAM_CARD_ACTION"
                    and item.get("userId") == user_id
                )
        if not allowed:
            raise RuntimeError("expired record is not allowlisted")
        return {"PK": pk, "SK": sk}, ttl

    def sweep(self, *, now: int | None = None) -> dict[str, object]:
        value = self._now() if now is None else now
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError("retention clock is invalid")
        now = value
        lease = self._cursors.load("expiry")
        if not isinstance(lease, SweepCursorLease) or lease.name != "expiry":
            raise RuntimeError("retention sweep cursor lease is invalid")
        start_key = lease.cursor
        deleted = 0
        seen = 0
        seen_cursors = {
            tuple(start_key[field] for field in _SWEEP_CURSOR_FIELDS["expiry"])
        } if start_key is not None else set()
        for _page in range(self._max_pages):
            remaining = self.MAX_ITEMS - seen
            if remaining <= 0:
                return {"status": "ok", "expired": deleted}
            page_limit = min(100, remaining)
            request = {
                "FilterExpression": "#ttl <= :now",
                "ProjectionExpression": (
                    "PK, SK, #ttl, #state, #recordType, userId"
                ),
                "ExpressionAttributeNames": {
                    "#ttl": "ttl",
                    "#state": "state",
                    "#recordType": "recordType",
                },
                "ExpressionAttributeValues": {":now": now},
                "Limit": page_limit,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            response = self._table.scan(**request)
            items = response.get("Items") if isinstance(response, Mapping) else None
            if not isinstance(items, list) or len(items) > page_limit:
                raise RuntimeError("retention scan returned invalid data")
            validated = [self._key(item, now=now) for item in items]
            seen += len(validated)
            next_cursor = _sweep_cursor(
                "expiry",
                response.get("LastEvaluatedKey"),
            )
            if next_cursor is not None:
                signature = tuple(
                    next_cursor[field]
                    for field in _SWEEP_CURSOR_FIELDS["expiry"]
                )
                if signature in seen_cursors:
                    raise RuntimeError("retention pagination is invalid")
                seen_cursors.add(signature)
            for key, ttl in validated:
                try:
                    self._table.delete_item(
                        Key=key,
                        ConditionExpression="#ttl=:ttl",
                        ExpressionAttributeNames={"#ttl": "ttl"},
                        ExpressionAttributeValues={":ttl": ttl},
                    )
                    deleted += 1
                except Exception as error:
                    if not _conditional_failure(error):
                        raise RuntimeError("expired record deletion is uncertain") from error
            if next_cursor != start_key or start_key is not None:
                lease = self._cursors.save(lease, next_cursor)
                if (
                    not isinstance(lease, SweepCursorLease)
                    or lease.name != "expiry"
                    or lease.cursor != next_cursor
                ):
                    raise RuntimeError("retention sweep cursor lease is invalid")
            if next_cursor is None:
                return {"status": "ok", "expired": deleted}
            if seen >= self.MAX_ITEMS:
                return {"status": "ok", "expired": deleted}
            start_key = next_cursor
        return {"status": "ok", "expired": deleted}


class DeletionCoordinator:
    """Two-pass deletion with a durable authority fence and invocation drain."""

    # The longest queued product worker is bounded to 600 seconds and AWS STS
    # credentials cannot be shorter than 900 seconds. Thirty minutes outlives
    # both the minimum credential lifetime and an issuance race at the first
    # deletion fence before the second exact purge removes late writes.
    FINALIZATION_GRACE_MS = 30 * 60 * 1_000

    def __init__(
        self,
        *,
        session_store,
        connection_store,
        runtime_driver,
        workspace_store,
        record_store,
        footprint_store,
        clock_ms=None,
        finalization_grace_ms: int = FINALIZATION_GRACE_MS,
    ) -> None:
        if (
            isinstance(finalization_grace_ms, bool)
            or not isinstance(finalization_grace_ms, int)
            or finalization_grace_ms < self.FINALIZATION_GRACE_MS
        ):
            raise ValueError("deletion finalization grace is below the safety bound")
        self._sessions = session_store
        self._connections = connection_store
        self._runtime = runtime_driver
        self._workspace = workspace_store
        self._records = record_store
        if not callable(getattr(footprint_store, "delete_user_records", None)):
            raise TypeError("external user footprint store is invalid")
        self._footprint = footprint_store
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._finalization_grace_ms = finalization_grace_ms

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise DeletionPending("deletion reconciliation clock is invalid")
        return value

    @staticmethod
    def _intent(value: object, *, user_id: str) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise DeletionPending("deletion intent is invalid")
        status = value.get("deletionStatus")
        requested_at = value.get("requestedAt")
        finalizing_at = value.get("finalizingAt")
        completed_at = value.get("completedAt")
        invalid = (
            value.get("userId") != user_id
            or value.get("purgeReason") != "ACCOUNT_DELETION"
            or status not in {"PENDING", "FINALIZING", "COMPLETED"}
            or (
                status == "PENDING"
                and (
                    isinstance(requested_at, bool)
                    or not isinstance(requested_at, int)
                    or requested_at <= 0
                    or finalizing_at is not None
                    or completed_at is not None
                )
            )
            or (
                status == "FINALIZING"
                and (
                    isinstance(requested_at, bool)
                    or not isinstance(requested_at, int)
                    or requested_at <= 0
                    or isinstance(finalizing_at, bool)
                    or not isinstance(finalizing_at, int)
                    or finalizing_at < requested_at
                    or completed_at is not None
                )
            )
            or (
                status == "COMPLETED"
                and (
                    requested_at is not None
                    or finalizing_at is not None
                    or isinstance(completed_at, bool)
                    or not isinstance(completed_at, int)
                    or completed_at <= 0
                )
            )
        )
        if invalid:
            raise DeletionPending("deletion intent is invalid")
        return dict(value)

    def _begin(self, user_id: str) -> dict[str, object]:
        try:
            return self._intent(
                self._sessions.begin_deletion(user_id),
                user_id=user_id,
            )
        except DeletionPending:
            raise
        except Exception as error:
            raise DeletionPending("deletion intent persistence is uncertain") from error

    def _get(self, user_id: str) -> dict[str, object]:
        try:
            return self._intent(
                self._sessions.get_deletion_intent(user_id),
                user_id=user_id,
            )
        except DeletionPending:
            raise
        except Exception as error:
            raise DeletionPending("deletion intent lookup is uncertain") from error

    def _purge_once(self, user_id: str) -> None:
        """Repeatable exact authority revocation and user-byte removal."""

        try:
            self._sessions.revoke_all(user_id)
            self._connections.revoke_all(user_id)
            runtime = self._runtime.purge(user_id)
            if (
                not isinstance(runtime, Mapping)
                or runtime.get("userId") != user_id
                or runtime.get("state") != "DELETING"
                or runtime.get("purgeReason") != "ACCOUNT_DELETION"
                or isinstance(runtime.get("purgeCompletedAt"), bool)
                or not isinstance(runtime.get("purgeCompletedAt"), int)
                or runtime["purgeCompletedAt"] <= 0
            ):
                raise RuntimeError("runtime account purge returned invalid data")
            self._workspace.delete_namespace(user_id)
            self._records.delete_user_records(user_id)
            self._footprint.delete_user_records(user_id)
        except Exception as error:
            raise DeletionPending("account deletion requires reconciliation") from error

    def delete(self, user_id: str) -> dict[str, str]:
        user_id = _user_id(user_id)
        intent = self._begin(user_id)
        if intent.get("deletionStatus") == "COMPLETED":
            return {"status": "deleted", "userId": user_id}
        if intent.get("deletionStatus") == "FINALIZING":
            raise DeletionPending("account deletion is awaiting final cleanup")
        self._purge_once(user_id)
        try:
            finalizing = self._intent(
                self._sessions.mark_deletion_finalizing(user_id),
                user_id=user_id,
            )
        except Exception as error:
            if isinstance(error, DeletionPending):
                raise
            raise DeletionPending("deletion finalization fence is uncertain") from error
        if finalizing["deletionStatus"] == "COMPLETED":
            return {"status": "deleted", "userId": user_id}
        if finalizing["deletionStatus"] != "FINALIZING":
            raise DeletionPending("deletion finalization fence is invalid")
        # The HTTP boundary maps this to Accepted/Pending. Completion is only
        # truthful after a later scheduled second purge.
        raise DeletionPending("account deletion is awaiting final cleanup")

    def reconcile(self, user_id: str) -> dict[str, str]:
        """Advance PENDING once, then FINALIZING only after the drain grace."""

        user_id = _user_id(user_id)
        intent = self._begin(user_id)
        status = intent["deletionStatus"]
        if status == "COMPLETED":
            return {"status": "deleted", "userId": user_id}
        if status == "PENDING":
            try:
                self.delete(user_id)
            except DeletionPending:
                refreshed = self._get(user_id)
                if refreshed["deletionStatus"] == "COMPLETED":
                    return {"status": "deleted", "userId": user_id}
                if refreshed["deletionStatus"] != "FINALIZING":
                    raise
                return {"status": "pending", "userId": user_id}
            raise DeletionPending("pending deletion advanced without a finalization fence")

        now = self._now_ms()
        finalizing_at = intent["finalizingAt"]
        assert isinstance(finalizing_at, int)
        if now - finalizing_at < self._finalization_grace_ms:
            return {"status": "pending", "userId": user_id}

        # All trusted command/web invocations that could have observed the old
        # account authority have now expired. Repeat every revocation and purge
        # before atomically marking the durable intent complete.
        self._purge_once(user_id)
        try:
            completed = self._intent(
                self._sessions.complete_deletion(
                    user_id,
                    finalizing_before_ms=now - self._finalization_grace_ms,
                ),
                user_id=user_id,
            )
        except Exception as error:
            if isinstance(error, DeletionPending):
                raise
            raise DeletionPending("deletion completion marker is uncertain") from error
        if completed["deletionStatus"] != "COMPLETED":
            raise DeletionPending("deletion completion marker is invalid")
        return {"status": "deleted", "userId": user_id}

    def delete_inactive(
        self,
        user_id: str,
        *,
        observed_updated_at_ms: int,
        observed_revision: int,
        inactive_before_ms: int,
    ) -> dict[str, str]:
        """Expire only runtime/workspace after winning the inactivity fence."""

        user_id = _user_id(user_id)
        values = {
            "observed runtime millisecond": observed_updated_at_ms,
            "observed runtime revision": observed_revision,
            "inactive cutoff millisecond": inactive_before_ms,
        }
        for label, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} is invalid")
        if observed_updated_at_ms > inactive_before_ms:
            raise ValueError("observed runtime is not inactive")
        try:
            runtime = self._runtime.purge_inactive(
                user_id,
                observed_updated_at_ms=observed_updated_at_ms,
                observed_revision=observed_revision,
                inactive_before_ms=inactive_before_ms,
            )
        except Exception as error:
            raise DeletionPending(
                "inactive runtime purge requires reconciliation"
            ) from error
        if runtime is None:
            return {"status": "active", "userId": user_id}
        if (
            not isinstance(runtime, Mapping)
            or runtime.get("userId") != user_id
            or runtime.get("state") != "DELETING"
            or runtime.get("purgeReason") != "WORKSPACE_EXPIRY"
        ):
            raise DeletionPending("inactive runtime purge returned invalid data")
        try:
            self._workspace.delete_namespace(user_id)
            completed = self._runtime.complete_inactive_purge(user_id)
        except Exception as error:
            raise DeletionPending(
                "workspace expiry requires reconciliation"
            ) from error
        if (
            not isinstance(completed, Mapping)
            or completed.get("userId") != user_id
            or completed.get("state") != "COLD"
            or completed.get("tombstonedAt") is not None
            or completed.get("purgeReason") is not None
        ):
            raise DeletionPending("workspace expiry completion is invalid")
        return {"status": "expired", "userId": user_id}


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        raise ExportBoundaryError("workspace path is invalid")
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or not parts
    ):
        raise ExportBoundaryError("workspace path is invalid")
    return path.as_posix()


class UserExporter:
    # Lambda synchronous responses are capped at 6 MiB. API Gateway requires
    # binary responses to be base64 encoded, adding roughly one third. Keep a
    # substantial fixed margin for the proxy envelope and response headers.
    MAX_SYNC_ARCHIVE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        source,
        *,
        max_files: int = 1_000,
        max_entry_bytes: int = 5 * 1024 * 1024,
        max_total_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self._source = source
        self._max_files = max_files
        self._max_entry = max_entry_bytes
        self._max_total = max_total_bytes

    def build_zip(self, user_id: str) -> bytes:
        user_id = _user_id(user_id)
        records = self._source.records_for_user(user_id)
        files = self._source.workspace_files(user_id)
        if not isinstance(records, Mapping) or not isinstance(files, Mapping):
            raise ExportBoundaryError("export source returned invalid data")
        if not set(records).issubset(_EXPORT_CATEGORIES):
            raise ExportBoundaryError("record category is not exportable")
        entries: dict[str, bytes] = {}
        for category in sorted(records):
            try:
                payload = json.dumps(
                    records[category],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            except (TypeError, ValueError) as error:
                raise ExportBoundaryError("record export is not JSON") from error
            entries[f"records/{category}.json"] = payload
        for path, content in files.items():
            safe = _safe_path(path)
            if not isinstance(content, bytes):
                raise ExportBoundaryError("workspace export content must be bytes")
            entries[f"workspace/{safe}"] = content
        manifest = {
            "format": "personal-operator.export.v1",
            "userId": user_id,
            "entries": sorted(entries),
        }
        entries["manifest.json"] = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(entries) > self._max_files:
            raise ExportBoundaryError("export contains too many files")
        total = 0
        for content in entries.values():
            if len(content) > self._max_entry:
                raise ExportBoundaryError("export entry exceeds its size limit")
            total += len(content)
        if total > self._max_total:
            raise ExportBoundaryError("export exceeds its total size limit")
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for path in sorted(entries):
                info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, entries[path])
        result = output.getvalue()
        if len(result) > self.MAX_SYNC_ARCHIVE_BYTES:
            raise ExportBoundaryError(
                "export archive exceeds the synchronous delivery limit"
            )
        return result
