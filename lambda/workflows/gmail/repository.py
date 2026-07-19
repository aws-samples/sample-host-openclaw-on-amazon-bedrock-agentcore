"""DynamoDB persistence for the trusted read-only Gmail workflow.

One injected DynamoDB ``Table`` can serve four deliberately separate record
shapes: one-time OAuth state, encrypted connection envelopes, a replaceable
derived opportunity set, and immutable draft revisions. Raw Gmail responses
are not accepted by any write method.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import re
from typing import Mapping, Sequence

try:
    from .models import DraftRevision
except ImportError:  # direct file loading in Lambda/unit tests
    from gmail_models import DraftRevision


READONLY_PROVIDER = "google-gmail-readonly"
DERIVED_RECORD_TTL = timedelta(days=14)
_ACTION_TERMINAL_RETENTION_SECONDS = 90 * 24 * 60 * 60
_STATE_KEY = re.compile(r"^[0-9a-f]{64}$")
_CALLBACK_SK = re.compile(r"^TELEGRAM_CALLBACK#[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^gmail:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")
_ACTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_ENVELOPE_FIELDS = {
    "format",
    "binding",
    "wrappedKey",
    "nonce",
    "ciphertext",
}
_OPPORTUNITY_FIELDS = {
    "id",
    "userId",
    "source",
    "waitingSince",
    "title",
    "reason",
    "confidence",
}
_SOURCE_FIELDS = {
    "sourceId",
    "threadId",
    "deepLink",
    "correspondent",
    "subject",
    "excerpt",
}
_OAUTH_STATE_FIELDS = {
    "user_id",
    "redirect_uri",
    "code_verifier",
    "expires_at",
}
_FENCE_FIELDS = {
    "PK",
    "SK",
    "recordType",
    "userId",
    "generation",
    "status",
    "updatedAt",
}
_FENCE_SK = "GMAIL#CONNECTION_FENCE"
_FENCE_STATUSES = frozenset({"CONNECTED", "DISCONNECTING", "DISCONNECTED"})


class RepositoryRecordError(ValueError):
    """A caller attempted to persist data outside the frozen record schema."""


class DuplicateOAuthStateError(RepositoryRecordError):
    """The one-time OAuth state key already exists."""


class DraftRevisionConflictError(RepositoryRecordError):
    """An immutable action revision already contains another exact payload."""


class ConnectionFenceError(RuntimeError):
    """A Gmail writer lost to a local disconnect generation change."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise RepositoryRecordError(f"{label} is invalid")
    return value


def _text(
    value: object,
    label: str,
    limit: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise RepositoryRecordError(f"{label} is invalid")
    if not allow_empty and not value:
        raise RepositoryRecordError(f"{label} is invalid")
    return value


def _ttl(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RepositoryRecordError("ttl must be a positive epoch second")
    return value


def _aware_iso(value: object, label: str) -> str:
    value = _text(value, label, 64)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RepositoryRecordError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise RepositoryRecordError(f"{label} must be timezone-aware")
    return value


def _decimal_safe(value):
    """Convert JSON-style floats to DynamoDB-compatible exact decimals."""

    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [_decimal_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _decimal_safe(item) for key, item in value.items()}
    return value


def _generation(value: object) -> int:
    if isinstance(value, bool):
        raise RepositoryRecordError("connection generation is invalid")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise RepositoryRecordError("connection generation is invalid")
        value = int(value)
    if not isinstance(value, int) or value < 0:
        raise RepositoryRecordError("connection generation is invalid")
    return value


def _dynamo_value(value: object) -> dict[str, object]:
    """Serialize the repository's closed record types for low-level transactions."""

    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise RepositoryRecordError("DynamoDB number is invalid")
        return {"N": str(value)}
    if value is None:
        return {"NULL": True}
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RepositoryRecordError("DynamoDB map key is invalid")
        return {
            "M": {key: _dynamo_value(field) for key, field in value.items()}
        }
    if isinstance(value, (list, tuple)):
        return {"L": [_dynamo_value(field) for field in value]}
    raise RepositoryRecordError("DynamoDB value is invalid")


def _dynamo_item(value: Mapping[str, object]) -> dict[str, dict[str, object]]:
    return {name: _dynamo_value(field) for name, field in value.items()}


def _is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    return isinstance(details, Mapping) and details.get("Code") == (
        "ConditionalCheckFailedException"
    )


def _validate_oauth_state(value: Mapping[str, object], expires_at: int) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) not in {
        frozenset(_OAUTH_STATE_FIELDS),
        frozenset({*_OAUTH_STATE_FIELDS, "connection_generation"}),
    }:
        raise RepositoryRecordError("OAuth state record has invalid fields")
    state_expiry = _aware_iso(value["expires_at"], "expires_at")
    if int(datetime.fromisoformat(state_expiry).timestamp()) != expires_at:
        raise RepositoryRecordError("OAuth state TTL does not match its expiry")
    result = {
        "user_id": _identifier(value["user_id"], "user_id"),
        "redirect_uri": _text(value["redirect_uri"], "redirect_uri", 1_024),
        "code_verifier": _text(value["code_verifier"], "code_verifier", 256),
        "expires_at": state_expiry,
    }
    if "connection_generation" in value:
        result["connection_generation"] = _generation(
            value["connection_generation"]
        )
    return result


def _validate_envelope(record: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(record, Mapping) or set(record) != _ENVELOPE_FIELDS:
        raise RepositoryRecordError("token envelope has invalid fields")
    if record.get("format") != "personal-operator.oauth-envelope.v1":
        raise RepositoryRecordError("token envelope has an unknown format")
    binding = _text(record["binding"], "binding", 64)
    if re.fullmatch(r"[0-9a-f]{64}", binding) is None:
        raise RepositoryRecordError("token envelope binding is invalid")
    return {
        "format": record["format"],
        "binding": binding,
        "wrappedKey": _text(record["wrappedKey"], "wrappedKey", 131_072),
        "nonce": _text(record["nonce"], "nonce", 256),
        "ciphertext": _text(record["ciphertext"], "ciphertext", 131_072),
    }


def _validate_opportunity(
    record: Mapping[str, object], *, user_id: str
) -> dict[str, object]:
    if not isinstance(record, Mapping) or set(record) != _OPPORTUNITY_FIELDS:
        raise RepositoryRecordError("opportunity has invalid fields")
    if record.get("userId") != user_id:
        raise RepositoryRecordError("opportunity belongs to another user")
    source = record.get("source")
    if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
        raise RepositoryRecordError("opportunity source has invalid fields")
    source_id = _text(source["sourceId"], "sourceId", 270)
    if _SOURCE_ID.fullmatch(source_id) is None:
        raise RepositoryRecordError("sourceId is invalid")
    thread_id = _text(source["threadId"], "threadId", 128)
    deep_link = _text(source["deepLink"], "deepLink", 512)
    if deep_link != f"https://mail.google.com/mail/u/0/#inbox/{thread_id}":
        raise RepositoryRecordError("source deep link is invalid")
    confidence = record.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float, Decimal))
        or not 0 <= confidence <= 1
    ):
        raise RepositoryRecordError("opportunity confidence is invalid")
    return {
        "id": _text(record["id"], "id", 128),
        "userId": user_id,
        "source": {
            "sourceId": source_id,
            "threadId": thread_id,
            "deepLink": deep_link,
            "correspondent": _text(
                source["correspondent"], "correspondent", 320
            ),
            "subject": _text(
                source["subject"], "subject", 200, allow_empty=True
            ),
            "excerpt": _text(
                source["excerpt"], "excerpt", 280, allow_empty=True
            ),
        },
        "waitingSince": _aware_iso(record["waitingSince"], "waitingSince"),
        "title": _text(record["title"], "title", 120),
        "reason": _text(record["reason"], "reason", 280),
        "confidence": confidence,
    }


class DynamoGmailRepository:
    """Concrete DynamoDB adapter for OAuth, connections, and derived Gmail data."""

    def __init__(
        self,
        table,
        *,
        conditional_failure_types: tuple[type[BaseException], ...] = (),
        now=None,
    ) -> None:
        self._table = table
        self._table_name = getattr(table, "name", None)
        self._transaction_client = getattr(
            getattr(table, "meta", None), "client", None
        )
        self._conditional_failure_types = conditional_failure_types
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _derived_expiry(self, requested: object) -> int:
        requested = _ttl(requested)
        current = self._now()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise RepositoryRecordError("repository clock must be timezone-aware")
        exact = int(
            (
                current.astimezone(timezone.utc)
                + DERIVED_RECORD_TTL
            ).timestamp()
        )
        # The workflow computes the same TTL immediately before this call. A
        # one-second boundary is tolerated, but the repository always stores
        # its own exact fourteen-day value rather than trusting its caller.
        if abs(requested - exact) > 1:
            raise RepositoryRecordError("derived record TTL must be exactly 14 days")
        return exact

    def _now_epoch(self) -> int:
        current = self._now()
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise RepositoryRecordError("repository clock must be timezone-aware")
        return int(current.astimezone(timezone.utc).timestamp())

    @staticmethod
    def _connection_key(user_id: str) -> dict[str, str]:
        return {
            "PK": f"USER#{user_id}",
            "SK": f"CONNECTION#{READONLY_PROVIDER}",
        }

    @staticmethod
    def _fence_key(user_id: str) -> dict[str, str]:
        return {"PK": f"USER#{user_id}", "SK": _FENCE_SK}

    def _read_item(self, key: Mapping[str, str]) -> Mapping[str, object] | None:
        response = self._table.get_item(Key=dict(key), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    def _fence(self, user_id: str) -> tuple[int, str] | None:
        item = self._read_item(self._fence_key(user_id))
        if item is None:
            return None
        if (
            set(item) != _FENCE_FIELDS
            or item.get("PK") != f"USER#{user_id}"
            or item.get("SK") != _FENCE_SK
            or item.get("recordType") != "GMAIL_CONNECTION_FENCE"
            or item.get("userId") != user_id
            or item.get("status") not in _FENCE_STATUSES
        ):
            raise RepositoryRecordError("stored connection fence is invalid")
        generation = _generation(item.get("generation"))
        _generation(item.get("updatedAt"))
        return generation, str(item["status"])

    def oauth_generation(self, user_id: str) -> int:
        """Capture the generation for a new OAuth attempt.

        A reconnect may begin while disconnected, but never while a bounded
        disconnect purge is still in progress.
        """

        user_id = _identifier(user_id, "user_id")
        fence = self._fence(user_id)
        if fence is None:
            return 0
        generation, status = fence
        if status == "DISCONNECTING":
            raise ConnectionFenceError("connection disconnect is still pending")
        return generation

    def assert_generation(
        self,
        user_id: str,
        expected_generation: int,
        *,
        require_connected: bool = False,
    ) -> None:
        user_id = _identifier(user_id, "user_id")
        expected_generation = _generation(expected_generation)
        fence = self._fence(user_id)
        if fence is None:
            generation = 0
            connected = self._read_item(self._connection_key(user_id)) is not None
        else:
            generation, status = fence
            connected = status == "CONNECTED"
        if generation != expected_generation or (require_connected and not connected):
            raise ConnectionFenceError("connection generation changed")

    def connected_generation(self, user_id: str) -> int:
        user_id = _identifier(user_id, "user_id")
        fence = self._fence(user_id)
        if fence is None:
            if self._read_item(self._connection_key(user_id)) is None:
                raise ConnectionFenceError("Gmail connection is not active")
            return 0
        generation, status = fence
        if status != "CONNECTED":
            raise ConnectionFenceError("Gmail connection is not active")
        item = self._read_item(self._connection_key(user_id))
        if item is None:
            raise ConnectionFenceError("Gmail connection is not active")
        stored_generation = item.get("connectionGeneration")
        if stored_generation is not None and _generation(stored_generation) != generation:
            raise ConnectionFenceError("connection generation changed")
        if stored_generation is None and generation != 0:
            raise ConnectionFenceError("legacy connection is outside the active generation")
        return generation

    def _fence_item(self, user_id: str, generation: int, status: str) -> dict[str, object]:
        return {
            **self._fence_key(user_id),
            "recordType": "GMAIL_CONNECTION_FENCE",
            "userId": user_id,
            "generation": generation,
            "status": status,
            "updatedAt": self._now_epoch(),
        }

    def _set_existing_fence(
        self,
        user_id: str,
        *,
        expected_generation: int,
        expected_statuses: tuple[str, ...] | None = None,
        generation: int,
        status: str,
    ) -> None:
        condition = "generation=:expected"
        values: dict[str, object] = {
            ":expected": expected_generation,
            ":next": generation,
            ":status": status,
            ":now": self._now_epoch(),
        }
        if expected_statuses is not None:
            if not expected_statuses or any(
                candidate not in _FENCE_STATUSES
                for candidate in expected_statuses
            ):
                raise RepositoryRecordError("expected fence status is invalid")
            status_terms = []
            for index, candidate in enumerate(expected_statuses):
                placeholder = f":expectedStatus{index}"
                values[placeholder] = candidate
                status_terms.append(placeholder)
            condition += " AND #status IN (" + ", ".join(status_terms) + ")"
        self._table.update_item(
            Key=self._fence_key(user_id),
            UpdateExpression="SET generation=:next, #status=:status, updatedAt=:now",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )

    def _transact(self, operations: Sequence[Mapping[str, object]]) -> None:
        transact = getattr(self._transaction_client, "transact_write_items", None)
        if not isinstance(self._table_name, str) or not self._table_name or not callable(
            transact
        ):
            raise RepositoryRecordError(
                "repository table does not support atomic generation writes"
            )
        transact(TransactItems=list(operations))

    @staticmethod
    def _fence_allows(
        fence: tuple[int, str] | None,
        generation: int,
        statuses: tuple[str, ...],
    ) -> bool:
        return (
            fence is None
            and generation == 0
            or fence is not None
            and fence[0] == generation
            and fence[1] in statuses
        )

    def _guarded_put(
        self,
        *,
        user_id: str,
        expected_generation: int,
        allowed_statuses: tuple[str, ...],
        item: Mapping[str, object],
        target_condition: str | None = None,
        target_condition_names: Mapping[str, str] | None = None,
        conflict_error: type[Exception] | None = None,
    ) -> None:
        """Atomically bind one Put to the exact current connection fence."""

        fence = self._fence(user_id)
        if not self._fence_allows(fence, expected_generation, allowed_statuses):
            raise ConnectionFenceError("connection generation changed")
        if fence is None and allowed_statuses == ("CONNECTED",):
            connection = self._read_item(self._connection_key(user_id))
            if connection is None:
                raise ConnectionFenceError("Gmail connection is not active")

        condition: dict[str, object] = {
            "TableName": self._table_name,
            "Key": _dynamo_item(self._fence_key(user_id)),
        }
        if fence is None:
            condition["ConditionExpression"] = (
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            )
        else:
            condition["ConditionExpression"] = (
                "generation=:generation AND #status IN ("
                + ", ".join(
                    f":status{index}" for index in range(len(allowed_statuses))
                )
                + ")"
            )
            condition["ExpressionAttributeNames"] = {"#status": "status"}
            condition["ExpressionAttributeValues"] = {
                ":generation": _dynamo_value(expected_generation),
                **{
                    f":status{index}": _dynamo_value(status)
                    for index, status in enumerate(allowed_statuses)
                },
            }
        put: dict[str, object] = {
            "TableName": self._table_name,
            "Item": _dynamo_item(item),
        }
        if target_condition is not None:
            put["ConditionExpression"] = target_condition
        if target_condition_names is not None:
            put["ExpressionAttributeNames"] = dict(target_condition_names)
        try:
            self._transact([{"ConditionCheck": condition}, {"Put": put}])
            return
        except Exception as error:
            try:
                current_fence = self._fence(user_id)
            except Exception:
                raise ConnectionFenceError(
                    "connection write outcome is uncertain"
                ) from error
            if not self._fence_allows(
                current_fence, expected_generation, allowed_statuses
            ):
                raise ConnectionFenceError(
                    "connection write lost to disconnect"
                ) from None
            try:
                stored = self._read_item(
                    {"PK": str(item["PK"]), "SK": str(item["SK"])}
                )
            except Exception:
                raise ConnectionFenceError(
                    "connection write outcome is uncertain"
                ) from error
            if isinstance(stored, Mapping) and dict(stored) == dict(item):
                return
            if conflict_error is not None and stored is not None:
                raise conflict_error(
                    "draft revision already contains another payload"
                ) from None
            raise

    def activate_connection(self, user_id: str, expected_generation: int) -> None:
        user_id = _identifier(user_id, "user_id")
        expected_generation = _generation(expected_generation)
        fence = self._fence(user_id)
        try:
            if fence is None:
                if expected_generation != 0:
                    raise ConnectionFenceError("connection generation changed")
                self._transact(
                    [
                        {
                            "ConditionCheck": {
                                "TableName": self._table_name,
                                "Key": _dynamo_item(
                                    self._connection_key(user_id)
                                ),
                                "ConditionExpression": (
                                    "connectionGeneration=:generation"
                                ),
                                "ExpressionAttributeValues": {
                                    ":generation": _dynamo_value(0)
                                },
                            }
                        },
                        {
                            "Put": {
                                "TableName": self._table_name,
                                "Item": _dynamo_item(
                                    self._fence_item(
                                        user_id, expected_generation, "CONNECTED"
                                    )
                                ),
                                "ConditionExpression": (
                                    "attribute_not_exists(PK) AND "
                                    "attribute_not_exists(SK)"
                                ),
                            }
                        },
                    ]
                )
            else:
                self._transact(
                    [
                        {
                            "ConditionCheck": {
                                "TableName": self._table_name,
                                "Key": _dynamo_item(
                                    self._connection_key(user_id)
                                ),
                                "ConditionExpression": (
                                    "connectionGeneration=:generation"
                                ),
                                "ExpressionAttributeValues": {
                                    ":generation": _dynamo_value(
                                        expected_generation
                                    )
                                },
                            }
                        },
                        {
                            "Update": {
                                "TableName": self._table_name,
                                "Key": _dynamo_item(self._fence_key(user_id)),
                                "UpdateExpression": (
                                    "SET #status=:connected, updatedAt=:now"
                                ),
                                "ConditionExpression": (
                                    "generation=:generation AND #status IN "
                                    "(:disconnected, :connected)"
                                ),
                                "ExpressionAttributeNames": {
                                    "#status": "status"
                                },
                                "ExpressionAttributeValues": {
                                    ":generation": _dynamo_value(
                                        expected_generation
                                    ),
                                    ":disconnected": _dynamo_value(
                                        "DISCONNECTED"
                                    ),
                                    ":connected": _dynamo_value("CONNECTED"),
                                    ":now": _dynamo_value(self._now_epoch()),
                                },
                            }
                        },
                    ]
                )
        except Exception as error:
            try:
                if self.connected_generation(user_id) != expected_generation:
                    raise ConnectionFenceError("connection generation changed")
            except Exception:
                raise ConnectionFenceError("connection activation lost its generation") from error
        if self.connected_generation(user_id) != expected_generation:
            raise ConnectionFenceError("connection activation lost its generation")

    def begin_disconnect(self, user_id: str) -> int:
        user_id = _identifier(user_id, "user_id")
        fence = self._fence(user_id)
        if fence is not None and fence[1] == "DISCONNECTING":
            return fence[0]
        current = 0 if fence is None else fence[0]
        generation = current + 1
        try:
            if fence is None:
                self._table.put_item(
                    Item=self._fence_item(user_id, generation, "DISCONNECTING"),
                    ConditionExpression=(
                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                    ),
                )
            else:
                self._set_existing_fence(
                    user_id,
                    expected_generation=current,
                    expected_statuses=("CONNECTED", "DISCONNECTED"),
                    generation=generation,
                    status="DISCONNECTING",
                )
        except Exception as error:
            reconciled = self._fence(user_id)
            if reconciled is None or reconciled[1] != "DISCONNECTING":
                raise ConnectionFenceError("disconnect fence outcome is uncertain") from error
            return reconciled[0]
        self.assert_generation(user_id, generation)
        return generation

    def finish_disconnect(self, user_id: str, generation: int) -> None:
        user_id = _identifier(user_id, "user_id")
        generation = _generation(generation)
        try:
            self._set_existing_fence(
                user_id,
                expected_generation=generation,
                expected_statuses=("DISCONNECTING",),
                generation=generation,
                status="DISCONNECTED",
            )
        except Exception as error:
            reconciled = self._fence(user_id)
            if reconciled != (generation, "DISCONNECTED"):
                raise ConnectionFenceError("disconnect completion is uncertain") from error

    def delete_under_disconnecting_fence(
        self,
        user_id: str,
        generation: int,
        key: Mapping[str, str],
    ) -> None:
        """Delete one derived record only while this exact disconnect owns it.

        A disconnect purge reuses one ``DISCONNECTING`` generation, so a slow
        runner can resume after a faster runner completed and a reconnect
        activated a new generation. Every destructive step is therefore bound
        by transaction to the exact ``(generation, DISCONNECTING)`` fence: once
        the fence advances or flips to ``CONNECTED``, the delete fails closed
        and cannot touch the reconnected records.
        """

        user_id = _identifier(user_id, "user_id")
        generation = _generation(generation)
        if (
            not isinstance(key, Mapping)
            or set(key) != {"PK", "SK"}
            or key.get("PK") != f"USER#{user_id}"
            or not isinstance(key.get("SK"), str)
        ):
            raise RepositoryRecordError("fenced delete key is invalid")
        condition = {
            "TableName": self._table_name,
            "Key": _dynamo_item(self._fence_key(user_id)),
            "ConditionExpression": "generation=:generation AND #status=:status",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {
                ":generation": _dynamo_value(generation),
                ":status": _dynamo_value("DISCONNECTING"),
            },
        }
        delete = {
            "TableName": self._table_name,
            "Key": _dynamo_item({"PK": key["PK"], "SK": key["SK"]}),
        }
        try:
            self._transact([{"ConditionCheck": condition}, {"Delete": delete}])
        except Exception as error:
            try:
                fence = self._fence(user_id)
                still_present = (
                    self._read_item({"PK": key["PK"], "SK": key["SK"]}) is not None
                )
            except Exception as read_error:
                raise RuntimeError(
                    "connection purge outcome is uncertain"
                ) from read_error
            if fence == (generation, "DISCONNECTING"):
                # The fence still belongs to this exact attempt. Either the
                # write silently applied (target gone) or its outcome is
                # genuinely ambiguous and stays retryable under this pending
                # generation.
                if not still_present:
                    return
                raise RuntimeError(
                    "connection purge outcome is uncertain"
                ) from error
            # The fence advanced or flipped to CONNECTED: a reconnect or newer
            # disconnect now owns the record, so fail closed without deleting.
            raise ConnectionFenceError(
                "fenced delete lost to a disconnect generation change"
            ) from error

    def connection_status(self, user_id: str) -> str:
        user_id = _identifier(user_id, "user_id")
        fence = self._fence(user_id)
        if fence is not None and fence[1] != "CONNECTED":
            return "DISCONNECTED"
        return (
            "CONNECTED"
            if self._read_item(self._connection_key(user_id)) is not None
            else "DISCONNECTED"
        )

    def opportunities_match(
        self,
        user_id: str,
        generation: int,
        opportunities: Sequence[object],
    ) -> bool:
        """Bind Telegram handles to the exact current derived opportunity set."""

        user_id = _identifier(user_id, "user_id")
        generation = _generation(generation)
        self.assert_generation(user_id, generation, require_connected=True)
        item = self._read_item(
            {"PK": f"USER#{user_id}", "SK": "GMAIL#OPPORTUNITIES"}
        )
        if not isinstance(item, Mapping):
            return False
        stored_generation = item.get("connectionGeneration")
        if stored_generation is None:
            if generation != 0:
                return False
        elif _generation(stored_generation) != generation:
            return False
        records = item.get("opportunities")
        if not isinstance(records, list) or len(records) != len(opportunities):
            return False
        for record, opportunity in zip(records, opportunities, strict=True):
            source = record.get("source") if isinstance(record, Mapping) else None
            if (
                not isinstance(source, Mapping)
                or record.get("id") != getattr(opportunity, "id", None)
                or record.get("userId") != user_id
                or source.get("sourceId")
                != getattr(getattr(opportunity, "source", None), "source_id", None)
            ):
                return False
        self.assert_generation(user_id, generation, require_connected=True)
        return True

    def put_connected_record_once(
        self,
        *,
        user_id: str,
        generation: int,
        item: Mapping[str, object],
    ) -> None:
        """Atomically create one callback record under the connected fence."""

        user_id = _identifier(user_id, "user_id")
        generation = _generation(generation)
        if (
            not isinstance(item, Mapping)
            or item.get("PK") != f"USER#{user_id}"
            or not isinstance(item.get("SK"), str)
            or _CALLBACK_SK.fullmatch(item["SK"]) is None
            or item.get("connectionGeneration") != generation
        ):
            raise RepositoryRecordError("connected callback record is invalid")
        self._guarded_put(
            user_id=user_id,
            expected_generation=generation,
            allowed_statuses=("CONNECTED",),
            item=dict(item),
            target_condition=(
                "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            ),
        )

    # GoogleReadonlyOAuthFlow state-store interface.
    def put_once(
        self,
        key: str,
        value: Mapping[str, object],
        *,
        expires_at: int,
    ) -> None:
        if not isinstance(key, str) or _STATE_KEY.fullmatch(key) is None:
            raise RepositoryRecordError("OAuth state key is invalid")
        expires_at = _ttl(expires_at)
        state = _validate_oauth_state(value, expires_at)
        item = {
            "PK": f"OAUTH_STATE#{key}",
            "SK": "OAUTH_STATE",
            "userId": state["user_id"],
            "state": state,
            "ttl": expires_at,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except Exception as error:
            if isinstance(error, self._conditional_failure_types) or _is_conditional_failure(
                error
            ):
                raise DuplicateOAuthStateError("OAuth state already exists") from None
            raise

    def pop_once(self, key: str) -> Mapping[str, object] | None:
        if not isinstance(key, str) or _STATE_KEY.fullmatch(key) is None:
            raise RepositoryRecordError("OAuth state key is invalid")
        response = self._table.delete_item(
            Key={"PK": f"OAUTH_STATE#{key}", "SK": "OAUTH_STATE"},
            ReturnValues="ALL_OLD",
        )
        attributes = response.get("Attributes") if isinstance(response, Mapping) else None
        if not isinstance(attributes, Mapping):
            return None
        stored_expiry = _ttl(attributes.get("ttl"))
        if stored_expiry <= self._now_epoch():
            return None
        state = attributes.get("state")
        if not isinstance(state, Mapping):
            raise RepositoryRecordError("stored OAuth state is invalid")
        return dict(state)

    # KmsEnvelopeTokenVault record-store interface. The item deliberately has
    # no TTL: the encrypted refresh-token connection outlives derived records.
    def put(
        self,
        *,
        user_id: str,
        provider: str,
        record: Mapping[str, object],
        expected_generation: int | None = None,
        allow_disconnected: bool = False,
    ) -> None:
        user_id = _identifier(user_id, "user_id")
        if provider != READONLY_PROVIDER:
            raise RepositoryRecordError("provider is not Gmail read-only")
        item = {
            "PK": f"USER#{user_id}",
            "SK": f"CONNECTION#{provider}",
            "envelope": _validate_envelope(record),
        }
        if expected_generation is not None:
            expected_generation = _generation(expected_generation)
            item["connectionGeneration"] = expected_generation
            self._guarded_put(
                user_id=user_id,
                expected_generation=expected_generation,
                allowed_statuses=("CONNECTED", "DISCONNECTED")
                if allow_disconnected
                else ("CONNECTED",),
                item=item,
            )
            return
        self._table.put_item(Item=item)

    def get(self, *, user_id: str, provider: str) -> Mapping[str, object] | None:
        user_id = _identifier(user_id, "user_id")
        if provider != READONLY_PROVIDER:
            raise RepositoryRecordError("provider is not Gmail read-only")
        item = self._read_item(self._connection_key(user_id))
        if not isinstance(item, Mapping):
            return None
        allowed = {"PK", "SK", "envelope", "connectionGeneration"}
        if not set(item).issubset(allowed) or not {"PK", "SK", "envelope"}.issubset(item):
            raise RepositoryRecordError("stored token envelope is invalid")
        fence = self._fence(user_id)
        stored_generation = item.get("connectionGeneration")
        if fence is not None:
            generation, status = fence
            if status != "CONNECTED":
                return None
            if stored_generation is None and generation != 0:
                return None
            if stored_generation is not None and _generation(stored_generation) != generation:
                return None
        envelope = item.get("envelope")
        if not isinstance(envelope, Mapping):
            raise RepositoryRecordError("stored token envelope is invalid")
        return _validate_envelope(envelope)

    # GmailPilotWorkflow derived-record interface.
    def replace_opportunities(
        self,
        *,
        user_id: str,
        records: Sequence[Mapping[str, object]],
        expires_at: int,
        expected_generation: int | None = None,
    ) -> None:
        user_id = _identifier(user_id, "user_id")
        expires_at = self._derived_expiry(expires_at)
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise RepositoryRecordError("opportunities must be a sequence")
        if len(records) > 3:
            raise RepositoryRecordError("at most three opportunities may be stored")
        validated = [
            _validate_opportunity(record, user_id=user_id) for record in records
        ]
        ids = [record["id"] for record in validated]
        if len(set(ids)) != len(ids):
            raise RepositoryRecordError("opportunity IDs must be unique")
        item = {
            "PK": f"USER#{user_id}",
            "SK": "GMAIL#OPPORTUNITIES",
            "opportunities": _decimal_safe(validated),
            "ttl": expires_at,
        }
        if expected_generation is not None:
            expected_generation = _generation(expected_generation)
            item["connectionGeneration"] = expected_generation
            self._guarded_put(
                user_id=user_id,
                expected_generation=expected_generation,
                allowed_statuses=("CONNECTED",),
                item=item,
            )
            return
        self._table.put_item(Item=item)

    def save_draft(
        self,
        *,
        user_id: str,
        draft: DraftRevision,
        expires_at: int,
        expected_generation: int | None = None,
    ) -> None:
        user_id = _identifier(user_id, "user_id")
        expires_at = self._derived_expiry(expires_at)
        if not isinstance(draft, DraftRevision):
            raise RepositoryRecordError("draft must be a validated DraftRevision")
        key = {
            "PK": f"USER#{user_id}",
            "SK": f"GMAIL#DRAFT#{draft.action_id}#{draft.revision:010d}",
        }
        exact_draft = {
            "actionId": draft.action_id,
            "revision": draft.revision,
            "to": draft.to,
            "subject": draft.subject,
            "body": draft.body,
            "payloadHash": draft.payload_hash,
        }
        item = {**key, "draft": exact_draft, "ttl": expires_at}
        if expected_generation is not None:
            expected_generation = _generation(expected_generation)
            item["connectionGeneration"] = expected_generation
            self._guarded_put(
                user_id=user_id,
                expected_generation=expected_generation,
                allowed_statuses=("CONNECTED",),
                item=item,
                target_condition=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
                conflict_error=DraftRevisionConflictError,
            )
            return
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                ),
            )
        except Exception as error:
            # Both a conditional replay and an ambiguous transport failure are
            # reconciled by a strongly consistent read. Only the exact payload
            # (including its SHA-256) makes the original operation successful.
            response = self._table.get_item(Key=key, ConsistentRead=True)
            item = response.get("Item") if isinstance(response, Mapping) else None
            stored = item.get("draft") if isinstance(item, Mapping) else None
            stored_generation = item.get("connectionGeneration") if isinstance(item, Mapping) else None
            if (
                isinstance(stored, Mapping)
                and dict(stored) == exact_draft
                and (
                    expected_generation is None
                    or _generation(stored_generation) == expected_generation
                )
            ):
                return
            if isinstance(stored, Mapping) or isinstance(
                error, self._conditional_failure_types
            ) or _is_conditional_failure(error):
                raise DraftRevisionConflictError(
                    "draft revision already contains another payload"
                ) from None
            raise

    def save_superseding_draft(
        self,
        *,
        user_id: str,
        action_id: str,
        draft: DraftRevision,
        expected_draft_revision: int,
        current_draft_revision: int,
        expires_at: int,
        expected_generation: int | None = None,
    ) -> dict[str, object]:
        """Atomically persist an edit and remove or exclude old authority.

        The immutable revision Put and the exact action-item fence share one
        DynamoDB transaction.  If no action exists, the transaction proves it
        is still absent.  If PREPARED or APPROVAL_PENDING exists, that same
        transaction moves it to STALE.  Approval creation/transition therefore
        contends on the identical ACTION item and cannot occupy the old gap
        between supersession and draft persistence.
        """

        user_id = _identifier(user_id, "user_id")
        if not isinstance(action_id, str) or _ACTION_ID.fullmatch(action_id) is None:
            raise RepositoryRecordError("action_id is invalid")
        expected_draft_revision = _generation(expected_draft_revision)
        current_draft_revision = _generation(current_draft_revision)
        if expected_draft_revision < 1:
            raise RepositoryRecordError("expected draft revision is invalid")
        if (
            not isinstance(draft, DraftRevision)
            or draft.action_id != action_id
            or draft.revision != current_draft_revision
            or current_draft_revision != expected_draft_revision + 1
        ):
            raise RepositoryRecordError("atomic edit requires an exact draft binding")
        expires_at = self._derived_expiry(expires_at)
        if expected_generation is not None:
            expected_generation = _generation(expected_generation)

        action_key = {
            "PK": f"USER#{user_id}",
            "SK": f"ACTION#{action_id}",
        }
        try:
            action = self._read_item(action_key)
        except Exception as error:
            raise RepositoryRecordError(
                "draft authority state is unavailable"
            ) from error

        now_epoch = self._now_epoch()
        operations: list[Mapping[str, object]] = []
        if expected_generation is not None:
            fence = self._fence(user_id)
            if not self._fence_allows(
                fence, expected_generation, ("CONNECTED",)
            ):
                raise ConnectionFenceError("connection generation changed")
            if fence is None:
                if self._read_item(self._connection_key(user_id)) is None:
                    raise ConnectionFenceError("Gmail connection is not active")
                connection_condition: dict[str, object] = {
                    "TableName": self._table_name,
                    "Key": _dynamo_item(self._fence_key(user_id)),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                    ),
                }
            else:
                connection_condition = {
                    "TableName": self._table_name,
                    "Key": _dynamo_item(self._fence_key(user_id)),
                    "ConditionExpression": (
                        "generation=:generation AND #status=:status"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":generation": _dynamo_value(expected_generation),
                        ":status": _dynamo_value("CONNECTED"),
                    },
                }
            operations.append({"ConditionCheck": connection_condition})

        transition_id = (
            "draftedit_"
            + hashlib.sha256(
                (
                    user_id
                    + "\0"
                    + draft.action_id
                    + "\0"
                    + str(expected_draft_revision)
                    + "\0"
                    + str(draft.revision)
                    + "\0"
                    + draft.payload_hash
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        transition_time = self._now()
        if not isinstance(transition_time, datetime) or transition_time.tzinfo is None:
            raise RepositoryRecordError("repository clock must be timezone-aware")
        transition_iso = transition_time.astimezone(timezone.utc).isoformat()
        stale_ttl = now_epoch + _ACTION_TERMINAL_RETENTION_SECONDS

        initial_action_state = None
        initial_action_revision = None
        expected_stale_action = None
        if action is None:
            operations.append(
                {
                    "ConditionCheck": {
                        "TableName": self._table_name,
                        "Key": _dynamo_item(action_key),
                        "ConditionExpression": (
                            "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                        ),
                    }
                }
            )
        else:
            if (
                action.get("PK") != action_key["PK"]
                or action.get("SK") != action_key["SK"]
                or action.get("actionId") != draft.action_id
                or action.get("userId") != user_id
            ):
                raise RepositoryRecordError("stored action binding is invalid")
            initial_action_state = action.get("state")
            if initial_action_state not in {
                "PREPARED",
                "APPROVAL_PENDING",
                "STALE",
            }:
                raise DraftRevisionConflictError(
                    "draft cannot change after approval authority advanced"
                )
            initial_action_revision = _generation(action.get("revision"))
            action_draft_revision = _generation(action.get("draftRevision"))
            action_ttl = _generation(action.get("ttl"))
            if (
                initial_action_revision < 1
                or action_ttl <= now_epoch
            ):
                raise DraftRevisionConflictError(
                    "draft action authority no longer matches this revision"
                )
            stale_draft_revision = expected_draft_revision
            stale_chain_condition = ""
            if initial_action_state == "STALE":
                stored_stale_revision = _generation(
                    action.get("staleDraftRevision")
                )
                stored_superseding_revision = _generation(
                    action.get("supersededByDraftRevision")
                )
                if (
                    action.get("staleReason") != "newer-draft-revision"
                    or stored_stale_revision != action_draft_revision
                    or stored_superseding_revision != expected_draft_revision
                ):
                    raise DraftRevisionConflictError(
                        "stale draft chain no longer matches this revision"
                    )
                stale_draft_revision = stored_stale_revision
                stale_chain_condition = (
                    " AND #staleReason=:staleReason AND "
                    "#staleDraftRevision=:staleDraftRevision AND "
                    "#supersededByDraftRevision=:expectedDraftRevision"
                )
            elif action_draft_revision != expected_draft_revision:
                raise DraftRevisionConflictError(
                    "draft action authority no longer matches this revision"
                )
            action_names = {
                "#actionId": "actionId",
                "#userId": "userId",
                "#state": "state",
                "#revision": "revision",
                "#draftRevision": "draftRevision",
                "#ttl": "ttl",
                "#updatedAt": "updatedAt",
                "#lastTransitionId": "lastTransitionId",
                "#staleAt": "staleAt",
                "#staleReason": "staleReason",
                "#staleDraftRevision": "staleDraftRevision",
                "#supersededByDraftRevision": "supersededByDraftRevision",
            }
            action_values = {
                ":actionId": draft.action_id,
                ":userId": user_id,
                ":expectedState": initial_action_state,
                ":staleState": "STALE",
                ":expectedRevision": initial_action_revision,
                ":nextRevision": initial_action_revision + 1,
                ":actionDraftRevision": action_draft_revision,
                ":staleDraftRevision": stale_draft_revision,
                ":currentDraftRevision": draft.revision,
                ":nowEpoch": now_epoch,
                ":updatedAt": transition_iso,
                ":transitionId": transition_id,
                ":staleReason": "newer-draft-revision",
                ":retentionTtl": stale_ttl,
            }
            if initial_action_state == "STALE":
                action_values[":expectedDraftRevision"] = (
                    expected_draft_revision
                )
            expected_stale_action = {
                **dict(action),
                "state": "STALE",
                "revision": initial_action_revision + 1,
                "updatedAt": transition_iso,
                "lastTransitionId": transition_id,
                "staleAt": transition_iso,
                "staleReason": "newer-draft-revision",
                "staleDraftRevision": stale_draft_revision,
                "supersededByDraftRevision": draft.revision,
                "ttl": stale_ttl,
            }
            operations.append(
                {
                    "Update": {
                        "TableName": self._table_name,
                        "Key": _dynamo_item(action_key),
                        "UpdateExpression": (
                            "SET #state=:staleState, #revision=:nextRevision, "
                            "#updatedAt=:updatedAt, "
                            "#lastTransitionId=:transitionId, "
                            "#staleAt=:updatedAt, #staleReason=:staleReason, "
                            "#staleDraftRevision=:staleDraftRevision, "
                            "#supersededByDraftRevision=:currentDraftRevision, "
                            "#ttl=:retentionTtl"
                        ),
                        "ConditionExpression": (
                            "#actionId=:actionId AND #userId=:userId AND "
                            "#state=:expectedState AND "
                            "#revision=:expectedRevision AND "
                            "#draftRevision=:actionDraftRevision AND "
                            "#ttl>:nowEpoch"
                            + stale_chain_condition
                        ),
                        "ExpressionAttributeNames": action_names,
                        "ExpressionAttributeValues": {
                            name: _dynamo_value(value)
                            for name, value in action_values.items()
                        },
                    }
                }
            )

        exact_draft = {
            "actionId": draft.action_id,
            "revision": draft.revision,
            "to": draft.to,
            "subject": draft.subject,
            "body": draft.body,
            "payloadHash": draft.payload_hash,
        }
        draft_item: dict[str, object] = {
            "PK": f"USER#{user_id}",
            "SK": f"GMAIL#DRAFT#{draft.action_id}#{draft.revision:010d}",
            "draft": exact_draft,
            "ttl": expires_at,
        }
        if expected_generation is not None:
            draft_item["connectionGeneration"] = expected_generation
        operations.append(
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _dynamo_item(draft_item),
                    "ConditionExpression": (
                        "attribute_not_exists(PK) AND attribute_not_exists(SK)"
                    ),
                }
            }
        )
        outcome = {
            "draftPersisted": True,
            "actionId": draft.action_id,
            "userId": user_id,
            "draftRevision": draft.revision,
            "payloadHash": draft.payload_hash,
        }
        try:
            self._transact(operations)
            return outcome
        except Exception as error:
            try:
                stored_draft = self._read_item(
                    {"PK": str(draft_item["PK"]), "SK": str(draft_item["SK"])}
                )
                current_action = self._read_item(action_key)
            except Exception:
                raise RepositoryRecordError(
                    "atomic draft edit outcome is unproven"
                ) from error
            draft_proven = (
                isinstance(stored_draft, Mapping)
                and dict(stored_draft) == draft_item
            )
            if initial_action_state is None:
                action_proven = current_action is None
            else:
                action_proven = (
                    isinstance(current_action, Mapping)
                    and expected_stale_action is not None
                    and dict(current_action) == expected_stale_action
                )
            if draft_proven and action_proven:
                return outcome
            if expected_generation is not None:
                try:
                    self.assert_generation(
                        user_id,
                        expected_generation,
                        require_connected=True,
                    )
                except Exception:
                    raise ConnectionFenceError(
                        "connection write lost to disconnect"
                    ) from None
            if draft_proven:
                raise RepositoryRecordError(
                    "atomic draft edit outcome is unproven"
                ) from error
            if isinstance(error, self._conditional_failure_types) or _is_conditional_failure(
                error
            ):
                raise DraftRevisionConflictError(
                    "draft edit lost its action authority fence"
                ) from None
            raise RepositoryRecordError(
                "atomic draft edit outcome is unproven"
            ) from error

    def latest_draft(
        self,
        *,
        user_id: str,
        action_id: str,
        expected_generation: int | None = None,
    ) -> DraftRevision | None:
        """Strongly read the latest live immutable revision for one action."""

        user_id = _identifier(user_id, "user_id")
        if not isinstance(action_id, str) or _ACTION_ID.fullmatch(action_id) is None:
            raise RepositoryRecordError("action_id is invalid")
        if expected_generation is not None:
            expected_generation = _generation(expected_generation)
        prefix = f"GMAIL#DRAFT#{action_id}#"
        try:
            response = self._table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_id}",
                    ":sk": prefix,
                },
                ConsistentRead=True,
                ScanIndexForward=False,
                Limit=1,
            )
        except Exception as error:
            raise RepositoryRecordError("latest draft read is unavailable") from error
        items = response.get("Items") if isinstance(response, Mapping) else None
        if not isinstance(items, list) or len(items) > 1:
            raise RepositoryRecordError("latest draft read returned an invalid result")
        if not items:
            return None
        item = items[0]
        fields = {"PK", "SK", "draft", "ttl"}
        if (
            not isinstance(item, Mapping)
            or set(item)
            not in {
                frozenset(fields),
                frozenset({*fields, "connectionGeneration"}),
            }
            or item.get("PK") != f"USER#{user_id}"
        ):
            raise RepositoryRecordError("stored draft record is invalid")
        stored = item.get("draft")
        if not isinstance(stored, Mapping) or set(stored) != {
            "actionId",
            "revision",
            "to",
            "subject",
            "body",
            "payloadHash",
        }:
            raise RepositoryRecordError("stored draft payload is invalid")
        revision = _generation(stored.get("revision"))
        if revision < 1 or item.get("SK") != f"{prefix}{revision:010d}":
            raise RepositoryRecordError("stored draft key is invalid")
        if stored.get("actionId") != action_id:
            raise RepositoryRecordError("stored draft action binding is invalid")
        ttl = _generation(item.get("ttl"))
        if ttl <= self._now_epoch():
            return None
        if expected_generation is not None:
            stored_generation = item.get("connectionGeneration")
            if (
                stored_generation is None
                or _generation(stored_generation) != expected_generation
            ):
                raise ConnectionFenceError("connection generation changed")
        try:
            draft = DraftRevision(
                action_id=stored["actionId"],
                revision=revision,
                to=stored["to"],
                subject=stored["subject"],
                body=stored["body"],
                payload_hash=stored["payloadHash"],
            )
        except (TypeError, ValueError):
            raise RepositoryRecordError("stored draft payload is invalid") from None
        if expected_generation is not None:
            self.assert_generation(
                user_id,
                expected_generation,
                require_connected=True,
            )
        return draft
