"""DynamoDB one-time ticket and opaque-session persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import time
from typing import Mapping


_DIGEST = re.compile(r"[0-9a-f]{64}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_DRAFT_RETURN = re.compile(r"/workspace\?draft=[A-Za-z0-9_-]{8,128}")
_STATIC_RETURN_PATHS = frozenset(
    {"/", "/connections", "/workspace", "/export", "/delete"}
)


class WebStoreError(RuntimeError):
    pass


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("store key is invalid")
    return value


def _user(value: object) -> str:
    if not isinstance(value, str) or _USER_ID.fullmatch(value) is None:
        raise ValueError("user identity is invalid")
    return value


def _ttl(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("record expiry is invalid")
    return value


def _session_record(record: object, expires_at: int) -> dict:
    if not isinstance(record, Mapping) or set(record) != {
        "userId", "csrfDigest", "createdAt", "revoked"
    }:
        raise ValueError("session record is invalid")
    user_id = _user(record["userId"])
    csrf = _digest(record["csrfDigest"])
    created = record["createdAt"]
    if isinstance(created, bool) or not isinstance(created, int) or created <= 0:
        raise ValueError("session creation time is invalid")
    if record["revoked"] is not False:
        raise ValueError("new session cannot be revoked")
    return {
        "userId": user_id,
        "csrfDigest": csrf,
        "createdAt": created,
        "revoked": False,
        "expiresAt": _ttl(expires_at),
    }


class DynamoWebStore:
    """Implements both connect-ticket and session store protocols."""

    def __init__(self, table, *, clock_ms=None) -> None:
        self._table = table
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    @staticmethod
    def _deletion_key(user_id: str) -> dict[str, str]:
        digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        return {"PK": f"DELETION#{digest}", "SK": "DELETION"}

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise WebStoreError("deletion clock is invalid")
        return value

    @staticmethod
    def _deletion_record(item: object, *, user_id: str) -> dict[str, object]:
        if not isinstance(item, Mapping):
            raise WebStoreError("deletion intent is missing")
        status = item.get("deletionStatus")
        requested_at = item.get("requestedAt")
        finalizing_at = item.get("finalizingAt")
        completed_at = item.get("completedAt")
        expected_pk = "DELETION#" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        if (
            item.get("PK") != expected_pk
            or item.get("SK") != "DELETION"
            or item.get("recordType") != "DELETION_INTENT"
            or item.get("purgeReason") != "ACCOUNT_DELETION"
            or status not in {"PENDING", "FINALIZING", "COMPLETED"}
            or (
                status == "PENDING"
                and (
                    item.get("userId") != user_id
                    or isinstance(requested_at, bool)
                    or not isinstance(requested_at, int)
                    or requested_at <= 0
                    or finalizing_at is not None
                    or completed_at is not None
                )
            )
            or (
                status == "FINALIZING"
                and (
                    item.get("userId") != user_id
                    or isinstance(requested_at, bool)
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
                    "userId" in item
                    or "requestedAt" in item
                    or "finalizingAt" in item
                    or "deletionStatusPk" in item
                    or "deletionStatusSk" in item
                    or isinstance(completed_at, bool)
                    or not isinstance(completed_at, int)
                    or completed_at <= 0
                )
            )
        ):
            raise WebStoreError("deletion intent is corrupt")
        return {
            "userId": user_id,
            "deletionStatus": status,
            "purgeReason": "ACCOUNT_DELETION",
            "requestedAt": requested_at if status != "COMPLETED" else None,
            "finalizingAt": finalizing_at if status != "COMPLETED" else None,
            "completedAt": completed_at,
        }

    def get_deletion_intent(self, user_id: str) -> Mapping | None:
        user_id = _user(user_id)
        response = self._table.get_item(
            Key=self._deletion_key(user_id),
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        if item is None:
            return None
        return self._deletion_record(item, user_id=user_id)

    def begin_deletion(self, user_id: str) -> Mapping:
        """Persist the account-deletion authority fence before other effects."""

        user_id = _user(user_id)
        now = self._now_ms()
        item = {
            **self._deletion_key(user_id),
            "recordType": "DELETION_INTENT",
            "userId": user_id,
            "purgeReason": "ACCOUNT_DELETION",
            "deletionStatus": "PENDING",
            "deletionStatusPk": "DELETION#PENDING",
            "deletionStatusSk": f"{now:020d}#{user_id}",
            "requestedAt": now,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
            )
            return self._deletion_record(item, user_id=user_id)
        except Exception as error:
            current = self.get_deletion_intent(user_id)
            if current is not None:
                return current
            raise WebStoreError("deletion intent persistence is uncertain") from error

    def mark_deletion_finalizing(self, user_id: str) -> Mapping:
        """Start the invocation-drain grace only after the first exact purge."""

        user_id = _user(user_id)
        current = self.get_deletion_intent(user_id)
        if current is None:
            raise WebStoreError("deletion intent is missing")
        if current["deletionStatus"] in {"FINALIZING", "COMPLETED"}:
            return current
        now = self._now_ms()
        try:
            response = self._table.update_item(
                Key=self._deletion_key(user_id),
                UpdateExpression=(
                    "SET deletionStatus=:finalizing, deletionStatusPk=:statusPk, "
                    "deletionStatusSk=:statusSk, finalizingAt=:now"
                ),
                ConditionExpression=(
                    "recordType=:recordType AND userId=:userId AND "
                    "purgeReason=:reason AND deletionStatus=:pending AND "
                    "attribute_not_exists(finalizingAt) AND "
                    "attribute_not_exists(completedAt)"
                ),
                ExpressionAttributeValues={
                    ":finalizing": "FINALIZING",
                    ":statusPk": "DELETION#FINALIZING",
                    ":statusSk": f"{now:020d}#{user_id}",
                    ":now": now,
                    ":recordType": "DELETION_INTENT",
                    ":userId": user_id,
                    ":reason": "ACCOUNT_DELETION",
                    ":pending": "PENDING",
                },
                ReturnValues="ALL_NEW",
            )
            item = response.get("Attributes") if isinstance(response, Mapping) else None
            return self._deletion_record(item, user_id=user_id)
        except Exception as error:
            reconciled = self.get_deletion_intent(user_id)
            if reconciled is not None and reconciled["deletionStatus"] in {
                "FINALIZING",
                "COMPLETED",
            }:
                return reconciled
            raise WebStoreError("deletion finalization fence is uncertain") from error

    def complete_deletion(
        self,
        user_id: str,
        *,
        finalizing_before_ms: int,
    ) -> Mapping:
        user_id = _user(user_id)
        if (
            isinstance(finalizing_before_ms, bool)
            or not isinstance(finalizing_before_ms, int)
            or finalizing_before_ms <= 0
        ):
            raise ValueError("deletion finalization cutoff is invalid")
        current = self.get_deletion_intent(user_id)
        if current is None:
            raise WebStoreError("deletion intent is missing")
        if current["deletionStatus"] == "COMPLETED":
            return current
        if (
            current["deletionStatus"] != "FINALIZING"
            or current["finalizingAt"] > finalizing_before_ms
        ):
            raise WebStoreError("deletion finalization grace has not elapsed")
        now = self._now_ms()
        try:
            response = self._table.update_item(
                Key=self._deletion_key(user_id),
                UpdateExpression=(
                    "SET deletionStatus=:completed, completedAt=:now "
                    "REMOVE userId, deletionStatusPk, deletionStatusSk, "
                    "requestedAt, finalizingAt"
                ),
                ConditionExpression=(
                    "recordType=:recordType AND userId=:userId AND "
                    "purgeReason=:reason AND deletionStatus=:finalizing AND "
                    "finalizingAt <= :finalizingBefore AND "
                    "attribute_not_exists(completedAt)"
                ),
                ExpressionAttributeValues={
                    ":completed": "COMPLETED",
                    ":now": now,
                    ":recordType": "DELETION_INTENT",
                    ":userId": user_id,
                    ":reason": "ACCOUNT_DELETION",
                    ":finalizing": "FINALIZING",
                    ":finalizingBefore": finalizing_before_ms,
                },
                ReturnValues="ALL_NEW",
            )
            item = response.get("Attributes") if isinstance(response, Mapping) else None
            return self._deletion_record(item, user_id=user_id)
        except Exception as error:
            reconciled = self.get_deletion_intent(user_id)
            if reconciled is not None and reconciled["deletionStatus"] == "COMPLETED":
                return reconciled
            raise WebStoreError("deletion completion is uncertain") from error

    def put_once(self, key: str, record: Mapping, *, expires_at: int) -> None:
        key = _digest(key)
        fields = set(record) if isinstance(record, Mapping) else set()
        if fields not in (
            {"userId", "nonce", "issuedAt"},
            {"userId", "nonce", "issuedAt", "returnPath"},
        ):
            raise ValueError("connect record is invalid")
        user_id = _user(record["userId"])
        nonce = record["nonce"]
        issued = record["issuedAt"]
        return_path = record.get("returnPath")
        if not isinstance(nonce, str) or not 32 <= len(nonce) <= 256:
            raise ValueError("connect nonce is invalid")
        if isinstance(issued, bool) or not isinstance(issued, int) or issued <= 0:
            raise ValueError("connect issue time is invalid")
        if return_path is not None and (
            not isinstance(return_path, str)
            or (
                return_path not in _STATIC_RETURN_PATHS
                and _DRAFT_RETURN.fullmatch(return_path) is None
            )
        ):
            raise ValueError("connect record return path is invalid")
        expiry = _ttl(expires_at)
        optional = {"returnPath": return_path} if return_path is not None else {}
        self._table.put_item(
            Item={
                "PK": f"CONNECT#{key}",
                "SK": "CONNECT",
                "userId": user_id,
                "nonce": nonce,
                "issuedAt": issued,
                **optional,
                "expiresAt": expiry,
                "ttl": expiry,
            },
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    def pop_once(self, key: str) -> Mapping | None:
        key = _digest(key)
        response = self._table.delete_item(
            Key={"PK": f"CONNECT#{key}", "SK": "CONNECT"},
            ReturnValues="ALL_OLD",
        )
        item = response.get("Attributes") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping):
            return None
        if item.get("PK") != f"CONNECT#{key}" or item.get("SK") != "CONNECT":
            raise WebStoreError("connect record binding is corrupt")
        result = {
            "userId": item.get("userId"),
            "nonce": item.get("nonce"),
            "issuedAt": item.get("issuedAt"),
            "expiresAt": item.get("expiresAt"),
        }
        if "returnPath" in item:
            result["returnPath"] = item.get("returnPath")
        return result

    def create(self, key: str, record: Mapping, *, expires_at: int) -> None:
        key = _digest(key)
        value = _session_record(record, expires_at)
        self._table.put_item(
            Item={
                "PK": f"SESSION#{key}",
                "SK": "SESSION",
                "sessionKey": key,
                **value,
                "ttl": value["expiresAt"],
            },
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    def _session(self, key: str) -> dict | None:
        key = _digest(key)
        response = self._table.get_item(
            Key={"PK": f"SESSION#{key}", "SK": "SESSION"},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        if item is None:
            return None
        if (
            not isinstance(item, Mapping)
            or item.get("sessionKey") != key
            or item.get("PK") != f"SESSION#{key}"
            or item.get("SK") != "SESSION"
        ):
            raise WebStoreError("session record binding is corrupt")
        return dict(item)

    def get(self, key: str) -> Mapping | None:
        item = self._session(key)
        if item is None:
            return None
        marker = self._table.get_item(
            Key={"PK": f"USER#{item.get('userId')}", "SK": "WEB_REVOKED"},
            ConsistentRead=True,
        )
        globally_revoked = isinstance(marker, Mapping) and isinstance(
            marker.get("Item"), Mapping
        )
        deletion = self.get_deletion_intent(_user(item.get("userId")))
        return {
            "userId": item.get("userId"),
            "csrfDigest": item.get("csrfDigest"),
            "createdAt": item.get("createdAt"),
            "revoked": (
                item.get("revoked") is True
                or globally_revoked
                or deletion is not None
            ),
            "expiresAt": item.get("expiresAt"),
        }

    def revoke(self, key: str) -> None:
        item = self._session(key)
        if item is None:
            return
        self._table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET revoked=:true",
            ConditionExpression="sessionKey=:key",
            ExpressionAttributeValues={":true": True, ":key": _digest(key)},
        )

    def revoke_all(self, user_id: str) -> None:
        user_id = _user(user_id)
        # This strongly-read marker is the revocation authority. Do not query
        # the shared userId GSI here: it contains actions, callbacks, OAuth
        # state, and the deletion intent as well as sessions. Physical removal
        # is handled by the bounded deletion stores after this fence exists.
        self._table.put_item(
            Item={
                "PK": f"USER#{user_id}",
                "SK": "WEB_REVOKED",
                "userId": user_id,
                "revoked": True,
            },
        )


class DynamoOAuthStateStore:
    """One-time PKCE state records in a namespace distinct from connect tickets."""

    def __init__(self, table) -> None:
        self._table = table

    @staticmethod
    def _record(record: object, expires_at: int) -> dict[str, object]:
        required = {"user_id", "redirect_uri", "code_verifier", "expires_at"}
        if not isinstance(record, Mapping) or set(record) not in {
            frozenset(required),
            frozenset({*required, "connection_generation"}),
        }:
            raise ValueError("OAuth state record is invalid")
        user_id = _user(record["user_id"])
        redirect_uri = record["redirect_uri"]
        verifier = record["code_verifier"]
        expiry_text = record["expires_at"]
        if (
            not isinstance(redirect_uri, str)
            or not redirect_uri.startswith("https://")
            or len(redirect_uri) > 1_024
            or "\x00" in redirect_uri
            or not isinstance(verifier, str)
            or not 43 <= len(verifier) <= 128
            or re.fullmatch(r"[A-Za-z0-9_-]+", verifier) is None
            or not isinstance(expiry_text, str)
        ):
            raise ValueError("OAuth state record is invalid")
        try:
            parsed = datetime.fromisoformat(expiry_text)
        except ValueError as error:
            raise ValueError("OAuth state expiry is invalid") from error
        if parsed.tzinfo is None or int(parsed.astimezone(timezone.utc).timestamp()) != _ttl(
            expires_at
        ):
            raise ValueError("OAuth state expiry is invalid")
        result = {
            "userId": user_id,
            "redirectUri": redirect_uri,
            "codeVerifier": verifier,
            "expiresAt": expiry_text,
            "ttl": expires_at,
        }
        if "connection_generation" in record:
            generation = record["connection_generation"]
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
            ):
                raise ValueError("OAuth state generation is invalid")
            result["connectionGeneration"] = generation
        return result

    def put_once(self, key: str, record: Mapping, *, expires_at: int) -> None:
        key = _digest(key)
        value = self._record(record, expires_at)
        self._table.put_item(
            Item={"PK": f"OAUTHSTATE#{key}", "SK": "OAUTHSTATE", **value},
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )

    def pop_once(self, key: str) -> Mapping | None:
        key = _digest(key)
        response = self._table.delete_item(
            Key={"PK": f"OAUTHSTATE#{key}", "SK": "OAUTHSTATE"},
            ReturnValues="ALL_OLD",
        )
        item = response.get("Attributes") if isinstance(response, Mapping) else None
        if item is None:
            return None
        if (
            not isinstance(item, Mapping)
            or item.get("PK") != f"OAUTHSTATE#{key}"
            or item.get("SK") != "OAUTHSTATE"
        ):
            raise WebStoreError("OAuth state binding is corrupt")
        result = {
            "user_id": item.get("userId"),
            "redirect_uri": item.get("redirectUri"),
            "code_verifier": item.get("codeVerifier"),
            "expires_at": item.get("expiresAt"),
        }
        if "connectionGeneration" in item:
            result["connection_generation"] = item.get("connectionGeneration")
        return result
