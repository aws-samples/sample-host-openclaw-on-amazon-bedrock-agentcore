"""Opaque one-time pilot invitations bound atomically to Telegram identity.

Only the caller receives the bearer returned by :meth:`issue`. Persistence and
all reconciliation paths use its SHA-256 digest.  Redemption creates the
complete bidirectional identity mapping in the same DynamoDB transaction that
consumes the invitation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import re
import time
from typing import Callable, Mapping


INVITE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_INVITE_TTL_SECONDS = 30 * 24 * 60 * 60

_TOKEN = re.compile(r"poi1_[A-Za-z0-9_-]{32}")
_CHANNEL_USER_ID = re.compile(r"[1-9][0-9]{0,19}")
_USER_ID = re.compile(r"user_[0-9a-f]{16}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class InviteRejected(ValueError):
    """The supplied bearer cannot authorize a pilot identity."""


class InviteStoreError(RuntimeError):
    """Invitation persistence has no safe, exact outcome."""


@dataclass(frozen=True, slots=True, repr=False)
class IssuedInvite:
    token: str
    expires_at: int

    def __post_init__(self) -> None:
        if _TOKEN.fullmatch(self.token) is None:
            raise ValueError("issued invite token is invalid")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, int)
            or self.expires_at <= 0
        ):
            raise ValueError("issued invite expiry is invalid")

    def __repr__(self) -> str:
        return f"IssuedInvite(token=<redacted>, expires_at={self.expires_at!r})"


@dataclass(frozen=True, slots=True)
class InviteRedemption:
    user_id: str
    created: bool

    def __post_init__(self) -> None:
        if _USER_ID.fullmatch(self.user_id) is None or not isinstance(
            self.created, bool
        ):
            raise ValueError("invite redemption result is invalid")


def _epoch(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InviteStoreError("invite clock is invalid")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise InviteRejected("pilot invitation is unavailable")
    return value


def _digest(value: str) -> str:
    result = hashlib.sha256(value.encode("ascii")).hexdigest()
    assert _DIGEST.fullmatch(result) is not None
    return result


def _key(token: str) -> dict[str, str]:
    return {"PK": f"PILOT_INVITE#{_digest(token)}", "SK": "INVITE"}


def _string_item(value: Mapping[str, str]) -> dict[str, dict[str, str]]:
    return {name: {"S": field} for name, field in value.items()}


def _value(value: str | int) -> dict[str, str]:
    if isinstance(value, int) and not isinstance(value, bool):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    raise TypeError("unsupported DynamoDB value")


class DynamoPilotInvites:
    """Issue, revoke, and atomically redeem pilot invitations."""

    def __init__(
        self,
        table,
        *,
        now: Callable[[], int] | None = None,
        random_bytes: Callable[[int], bytes] | None = None,
        conditional_failure_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        table_name = getattr(table, "name", None)
        client = getattr(getattr(table, "meta", None), "client", None)
        if (
            not isinstance(table_name, str)
            or not table_name
            or not callable(getattr(table, "put_item", None))
            or not callable(getattr(table, "get_item", None))
            or not callable(getattr(table, "update_item", None))
            or not callable(getattr(client, "transact_write_items", None))
        ):
            raise TypeError("invite table is invalid")
        if not isinstance(conditional_failure_types, tuple) or any(
            not isinstance(value, type) or not issubclass(value, BaseException)
            for value in conditional_failure_types
        ):
            raise TypeError("conditional failure types are invalid")
        self._table = table
        self._table_name = table_name
        self._client = client
        self._now = now or (lambda: int(time.time()))
        self._random = random_bytes or os.urandom
        self._conditional_failures = conditional_failure_types

    def _read(self, token: str) -> Mapping[str, object] | None:
        try:
            response = self._table.get_item(Key=_key(token), ConsistentRead=True)
        except Exception as error:
            raise InviteStoreError("pilot invitation state is unavailable") from error
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    @staticmethod
    def _channel(
        *, channel: object, channel_user_id: object, display_name: object
    ) -> tuple[str, str]:
        if channel != "telegram" or not isinstance(
            channel_user_id, str
        ) or _CHANNEL_USER_ID.fullmatch(channel_user_id) is None:
            raise InviteRejected("pilot invitation is unavailable")
        if (
            not isinstance(display_name, str)
            or len(display_name) > 128
            or "\x00" in display_name
            or any(ord(character) < 32 for character in display_name)
        ):
            display_name = channel_user_id
        return f"telegram:{channel_user_id}", display_name or channel_user_id

    def issue(self, *, ttl_seconds: int = INVITE_TTL_SECONDS) -> IssuedInvite:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 60 <= ttl_seconds <= MAX_INVITE_TTL_SECONDS
        ):
            raise ValueError("pilot invitation TTL is invalid")
        now = _epoch(self._now())
        random = self._random(24)
        if not isinstance(random, bytes) or len(random) != 24:
            raise InviteStoreError("pilot invitation randomness is invalid")
        opaque = base64.urlsafe_b64encode(random).decode("ascii").rstrip("=")
        token = f"poi1_{opaque}"
        if _TOKEN.fullmatch(token) is None:
            raise InviteStoreError("pilot invitation randomness is invalid")
        expires_at = now + ttl_seconds
        item = {
            **_key(token),
            "recordType": "PILOT_INVITE_V1",
            "status": "ISSUED",
            "issuedAt": now,
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
            try:
                existing = self._read(token)
            except InviteStoreError:
                existing = None
            if existing is not None or isinstance(error, self._conditional_failures):
                raise InviteStoreError("pilot invitation token collision") from None
            raise InviteStoreError("pilot invitation issue is uncertain") from None
        return IssuedInvite(token=token, expires_at=expires_at)

    def revoke(self, token: object) -> bool:
        token = _token(token)
        now = _epoch(self._now())
        try:
            response = self._table.update_item(
                Key=_key(token),
                UpdateExpression="SET #status=:revoked, revokedAt=:now",
                ConditionExpression=(
                    "recordType=:recordType AND #status=:issued AND #ttl>:now"
                ),
                ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":recordType": "PILOT_INVITE_V1",
                    ":issued": "ISSUED",
                    ":revoked": "REVOKED",
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            item = response.get("Attributes") if isinstance(response, Mapping) else None
            if (
                not isinstance(item, Mapping)
                or item.get("status") != "REVOKED"
                or item.get("revokedAt") != now
            ):
                raise InviteStoreError("pilot invitation revoke returned invalid state")
            return True
        except InviteStoreError:
            raise
        except Exception:
            item = self._read(token)
            if isinstance(item, Mapping) and item.get("status") == "REVOKED":
                return True
            if isinstance(item, Mapping) and item.get("status") in {
                "REDEEMED",
                "ISSUED",
            }:
                return False
            return False

    @staticmethod
    def _identity(channel_key: str) -> str:
        return f"user_{hashlib.sha256(channel_key.encode('utf-8')).hexdigest()[:16]}"

    @staticmethod
    def _channel_tombstone(channel_key: str) -> str:
        digest = hashlib.sha256(channel_key.encode("utf-8")).hexdigest()
        return f"CHANNEL_TOMBSTONE#{digest}"

    @staticmethod
    def _user_tombstone(user_id: str) -> str:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        return f"USER_TOMBSTONE#{digest}"

    def _reconciled(
        self,
        *,
        token: str,
        channel_key: str,
        actor_digest: str,
        user_id: str,
        created: bool,
        now: int,
    ) -> InviteRedemption | None:
        invitation = self._read(token)
        if (
            not isinstance(invitation, Mapping)
            or invitation.get("recordType") != "PILOT_INVITE_V1"
            or invitation.get("status") != "REDEEMED"
            or invitation.get("redeemedActorDigest") != actor_digest
            or invitation.get("userId") != user_id
            or isinstance(invitation.get("ttl"), bool)
            or not isinstance(invitation.get("ttl"), int)
            or invitation["ttl"] <= now
        ):
            return None
        try:
            response = self._table.get_item(
                Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"},
                ConsistentRead=True,
            )
        except Exception as error:
            raise InviteStoreError("pilot identity state is unavailable") from error
        mapping = response.get("Item") if isinstance(response, Mapping) else None
        if (
            not isinstance(mapping, Mapping)
            or mapping.get("PK") != f"CHANNEL#{channel_key}"
            or mapping.get("SK") != "PROFILE"
            or mapping.get("channel") != "telegram"
            or mapping.get("channelUserId") != channel_key.removeprefix("telegram:")
            or mapping.get("userId") != user_id
        ):
            return None
        return InviteRedemption(user_id=user_id, created=created)

    def redeem(
        self,
        token: object,
        *,
        channel: object,
        channel_user_id: object,
        display_name: object = "",
    ) -> InviteRedemption:
        token = _token(token)
        channel_key, display_name = self._channel(
            channel=channel,
            channel_user_id=channel_user_id,
            display_name=display_name,
        )
        now = _epoch(self._now())
        user_id = self._identity(channel_key)
        actor_digest = hashlib.sha256(channel_key.encode("utf-8")).hexdigest()
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        channel_id = channel_key.removeprefix("telegram:")
        profile = {
            "PK": f"USER#{user_id}",
            "SK": "PROFILE",
            "userId": user_id,
            "createdAt": now_iso,
            "displayName": display_name,
        }
        forward = {
            "PK": f"CHANNEL#{channel_key}",
            "SK": "PROFILE",
            "userId": user_id,
            "channel": "telegram",
            "channelUserId": channel_id,
            "displayName": display_name,
            "boundAt": now_iso,
        }
        backref = {
            "PK": f"USER#{user_id}",
            "SK": f"CHANNEL#{channel_key}",
            "userId": user_id,
            "channel": "telegram",
            "channelUserId": channel_id,
            "boundAt": now_iso,
        }
        transaction = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _string_item(_key(token)),
                    "UpdateExpression": (
                        "SET #status=:redeemed, redeemedAt=:now, "
                        "redeemedActorDigest=:actorDigest, userId=:userId"
                    ),
                    "ConditionExpression": (
                        "recordType=:recordType AND #status=:issued AND "
                        "#ttl>:now AND attribute_not_exists(redeemedActorDigest)"
                    ),
                    "ExpressionAttributeNames": {
                        "#status": "status",
                        "#ttl": "ttl",
                    },
                    "ExpressionAttributeValues": {
                        name: _value(value)
                        for name, value in {
                            ":recordType": "PILOT_INVITE_V1",
                            ":issued": "ISSUED",
                            ":redeemed": "REDEEMED",
                            ":now": now,
                            ":actorDigest": actor_digest,
                            ":userId": user_id,
                        }.items()
                    },
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": _string_item(
                        {
                            "PK": self._channel_tombstone(channel_key),
                            "SK": "TOMBSTONE",
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "ConditionCheck": {
                    "TableName": self._table_name,
                    "Key": _string_item(
                        {
                            "PK": self._user_tombstone(user_id),
                            "SK": "TOMBSTONE",
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
        transaction.extend(
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _string_item(item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
            for item in (profile, forward, backref)
        )
        try:
            self._client.transact_write_items(TransactItems=transaction)
            result = self._reconciled(
                token=token,
                channel_key=channel_key,
                actor_digest=actor_digest,
                user_id=user_id,
                created=True,
                now=now,
            )
            if result is None:
                raise InviteStoreError("pilot identity commit could not be proven")
            return result
        except InviteStoreError:
            raise
        except Exception as error:
            created = not isinstance(error, self._conditional_failures)
            result = self._reconciled(
                token=token,
                channel_key=channel_key,
                actor_digest=actor_digest,
                user_id=user_id,
                created=created,
                now=now,
            )
            if result is not None:
                return result
            raise InviteRejected("pilot invitation is unavailable") from None
