"""DynamoDB persistence for the trusted read-only Gmail workflow.

One injected DynamoDB ``Table`` can serve four deliberately separate record
shapes: one-time OAuth state, encrypted connection envelopes, a replaceable
derived opportunity set, and immutable draft revisions. Raw Gmail responses
are not accepted by any write method.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Mapping, Sequence

try:
    from .models import DraftRevision
except ImportError:  # direct file loading in Lambda/unit tests
    from gmail_models import DraftRevision


READONLY_PROVIDER = "google-gmail-readonly"
DERIVED_RECORD_TTL = timedelta(days=14)
_STATE_KEY = re.compile(r"^[0-9a-f]{64}$")
_CALLBACK_SK = re.compile(r"^TELEGRAM_CALLBACK#[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^gmail:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")
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
        generation: int,
        status: str,
    ) -> None:
        self._table.update_item(
            Key=self._fence_key(user_id),
            UpdateExpression="SET generation=:next, #status=:status, updatedAt=:now",
            ConditionExpression="generation=:expected",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":expected": expected_generation,
                ":next": generation,
                ":status": status,
                ":now": self._now_epoch(),
            },
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
        if (
            fence is not None
            and fence[1] == "DISCONNECTED"
            and self._read_item(self._connection_key(user_id)) is None
        ):
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
                generation=generation,
                status="DISCONNECTED",
            )
        except Exception as error:
            reconciled = self._fence(user_id)
            if reconciled != (generation, "DISCONNECTED"):
                raise ConnectionFenceError("disconnect completion is uncertain") from error

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
