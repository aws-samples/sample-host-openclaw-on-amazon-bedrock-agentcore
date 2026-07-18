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


class RepositoryRecordError(ValueError):
    """A caller attempted to persist data outside the frozen record schema."""


class DuplicateOAuthStateError(RepositoryRecordError):
    """The one-time OAuth state key already exists."""


class DraftRevisionConflictError(RepositoryRecordError):
    """An immutable action revision already contains another exact payload."""


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


def _is_conditional_failure(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    details = response.get("Error")
    return isinstance(details, Mapping) and details.get("Code") == (
        "ConditionalCheckFailedException"
    )


def _validate_oauth_state(value: Mapping[str, object], expires_at: int) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _OAUTH_STATE_FIELDS:
        raise RepositoryRecordError("OAuth state record has invalid fields")
    state_expiry = _aware_iso(value["expires_at"], "expires_at")
    if int(datetime.fromisoformat(state_expiry).timestamp()) != expires_at:
        raise RepositoryRecordError("OAuth state TTL does not match its expiry")
    return {
        "user_id": _identifier(value["user_id"], "user_id"),
        "redirect_uri": _text(value["redirect_uri"], "redirect_uri", 1_024),
        "code_verifier": _text(value["code_verifier"], "code_verifier", 256),
        "expires_at": state_expiry,
    }


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
    ) -> None:
        user_id = _identifier(user_id, "user_id")
        if provider != READONLY_PROVIDER:
            raise RepositoryRecordError("provider is not Gmail read-only")
        self._table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": f"CONNECTION#{provider}",
                "envelope": _validate_envelope(record),
            }
        )

    def get(self, *, user_id: str, provider: str) -> Mapping[str, object] | None:
        user_id = _identifier(user_id, "user_id")
        if provider != READONLY_PROVIDER:
            raise RepositoryRecordError("provider is not Gmail read-only")
        response = self._table.get_item(
            Key={
                "PK": f"USER#{user_id}",
                "SK": f"CONNECTION#{provider}",
            },
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping):
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
        self._table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "GMAIL#OPPORTUNITIES",
                "opportunities": _decimal_safe(validated),
                "ttl": expires_at,
            }
        )

    def save_draft(
        self,
        *,
        user_id: str,
        draft: DraftRevision,
        expires_at: int,
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
        try:
            self._table.put_item(
                Item={**key, "draft": exact_draft, "ttl": expires_at},
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
            if isinstance(stored, Mapping) and dict(stored) == exact_draft:
                return
            if isinstance(stored, Mapping) or isinstance(
                error, self._conditional_failure_types
            ) or _is_conditional_failure(error):
                raise DraftRevisionConflictError(
                    "draft revision already contains another payload"
                ) from None
            raise
