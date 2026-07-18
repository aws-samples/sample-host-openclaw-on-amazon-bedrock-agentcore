"""DynamoDB event claim, result, and at-most-one Telegram outbox ledger."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import time
from typing import Mapping

try:
    from router.message_queue import QueueEnvelope
except ImportError:
    from message_queue import QueueEnvelope


PROCESSING_LEASE_MS = 300_000
MAX_RESULT_CHARS = 3_500
_OWNER = re.compile(r"worker-[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class LedgerError(RuntimeError):
    pass


class EventIdentityCollision(LedgerError):
    pass


class LedgerFenceLost(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class LedgerClaim:
    key: str
    request_sha256: str
    state: str
    result: str | None = None
    owner: str | None = None
    epoch: int | None = None


def result_sha256(result: str) -> str:
    if not isinstance(result, str) or not result or len(result) > MAX_RESULT_CHARS:
        raise LedgerError("result must be non-empty bounded text")
    return hashlib.sha256(result.encode("utf-8")).hexdigest()


def _is_conditional(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, dict)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


class DynamoProcessingLedger:
    def __init__(self, table, *, clock_ms=None) -> None:
        self._table = table
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def _read(self, key: str) -> dict | None:
        response = self._table.get_item(
            Key={"eventId": key}, ConsistentRead=True
        )
        item = response.get("Item")
        return dict(item) if isinstance(item, Mapping) else None

    @staticmethod
    def _assert_binding(item: Mapping, envelope: QueueEnvelope) -> None:
        if (
            item.get("eventId") != envelope.trace_id
            or item.get("traceId") != envelope.trace_id
            or item.get("userId") != envelope.user_id
            or item.get("requestSha256") != envelope.request_sha256
        ):
            raise EventIdentityCollision(
                "same immutable event identity was used for different content"
            )

    @staticmethod
    def _claim(item: Mapping, *, claimed: bool = False) -> LedgerClaim:
        state = item.get("state")
        if not isinstance(state, str):
            raise LedgerError("ledger record has no state")
        return LedgerClaim(
            key=item["eventId"],
            request_sha256=item["requestSha256"],
            state="CLAIMED" if claimed else state,
            result=item.get("result"),
            owner=item.get("processingOwner") or item.get("deliveryOwner"),
            epoch=int(item.get("processingEpoch") or item.get("deliveryEpoch") or 0),
        )

    def claim_processing(self, envelope: QueueEnvelope, *, owner: str) -> LedgerClaim:
        if not isinstance(envelope, QueueEnvelope):
            raise TypeError("claim requires QueueEnvelope")
        if not isinstance(owner, str) or _OWNER.fullmatch(owner) is None:
            raise ValueError("processing owner is invalid")
        now = int(self._clock_ms())
        item = {
            "eventId": envelope.trace_id,
            "requestSha256": envelope.request_sha256,
            "userId": envelope.user_id,
            "traceId": envelope.trace_id,
            "state": "PROCESSING",
            "processingOwner": owner,
            "processingEpoch": 1,
            "claimExpiresAt": now + PROCESSING_LEASE_MS,
            "createdAt": now,
            "updatedAt": now,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(eventId)",
            )
            return self._claim(item, claimed=True)
        except Exception as error:
            current = self._read(envelope.trace_id)
            if current is None:
                raise LedgerError("processing claim outcome is unknown") from error
            self._assert_binding(current, envelope)
            if (
                current.get("state") == "PROCESSING"
                and current.get("processingOwner") == owner
                and int(current.get("processingEpoch", 0)) == 1
            ):
                return self._claim(current, claimed=True)
            if current.get("state") == "PROCESSING" and int(
                current.get("claimExpiresAt", 0)
            ) < now:
                current = self._expire_processing(current, now=now)
            return self._claim(current)

    def _expire_processing(self, current: Mapping, *, now: int) -> dict:
        values = {
            ":processing": "PROCESSING",
            ":uncertain": "PROCESSING_UNCERTAIN",
            ":owner": current.get("processingOwner"),
            ":epoch": int(current.get("processingEpoch", 0)),
            ":now": now,
        }
        try:
            response = self._table.update_item(
                Key={"eventId": current["eventId"]},
                UpdateExpression=(
                    "SET #state=:uncertain, updatedAt=:now, "
                    "uncertaintyReason=:reason REMOVE claimExpiresAt"
                ),
                ConditionExpression=(
                    "#state=:processing AND processingOwner=:owner AND "
                    "processingEpoch=:epoch AND claimExpiresAt < :now"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    **values,
                    ":reason": "processing-claim-expired",
                },
                ReturnValues="ALL_NEW",
            )
            return dict(response["Attributes"])
        except Exception as error:
            reconciled = self._read(current["eventId"])
            if reconciled and reconciled.get("state") == "PROCESSING_UNCERTAIN":
                return reconciled
            raise LedgerError("could not quarantine expired processing") from error

    def complete_result(self, claim: LedgerClaim, result: str) -> LedgerClaim:
        digest = result_sha256(result)
        if claim.state != "CLAIMED" or not claim.owner or claim.epoch is None:
            raise LedgerFenceLost("result requires an active processing claim")
        now = int(self._clock_ms())
        try:
            response = self._table.update_item(
                Key={"eventId": claim.key},
                UpdateExpression=(
                    "SET #state=:ready, result=:result, resultSha256=:sha, "
                    "updatedAt=:now REMOVE processingOwner, claimExpiresAt"
                ),
                ConditionExpression=(
                    "#state=:processing AND requestSha256=:requestSha AND "
                    "processingOwner=:owner AND processingEpoch=:epoch"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":ready": "RESULT_READY",
                    ":processing": "PROCESSING",
                    ":result": result,
                    ":sha": digest,
                    ":requestSha": claim.request_sha256,
                    ":owner": claim.owner,
                    ":epoch": claim.epoch,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            return self._claim(response["Attributes"])
        except Exception as error:
            current = self._read(claim.key)
            if (
                current
                and current.get("state") == "RESULT_READY"
                and current.get("requestSha256") == claim.request_sha256
                and current.get("resultSha256") == digest
                and current.get("result") == result
            ):
                return self._claim(current)
            raise LedgerFenceLost("result persistence outcome is uncertain") from error

    def mark_processing_uncertain(self, claim: LedgerClaim, *, error_type: str) -> None:
        if claim.state != "CLAIMED" or not claim.owner or claim.epoch is None:
            return
        now = int(self._clock_ms())
        try:
            self._table.update_item(
                Key={"eventId": claim.key},
                UpdateExpression=(
                    "SET #state=:uncertain, uncertaintyReason=:reason, "
                    "updatedAt=:now REMOVE claimExpiresAt"
                ),
                ConditionExpression=(
                    "#state=:processing AND processingOwner=:owner AND "
                    "processingEpoch=:epoch"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":uncertain": "PROCESSING_UNCERTAIN",
                    ":owner": claim.owner,
                    ":epoch": claim.epoch,
                    ":reason": str(error_type)[:128],
                    ":now": now,
                },
            )
        except Exception:
            return

    def begin_delivery(self, claim: LedgerClaim, *, owner: str) -> LedgerClaim:
        if claim.state != "RESULT_READY" or claim.result is None:
            raise LedgerFenceLost("delivery requires a ready result")
        if not isinstance(owner, str) or _OWNER.fullmatch(owner) is None:
            raise ValueError("delivery owner is invalid")
        now = int(self._clock_ms())
        try:
            response = self._table.update_item(
                Key={"eventId": claim.key},
                UpdateExpression=(
                    "SET #state=:inflight, deliveryOwner=:owner, "
                    "deliveryEpoch=if_not_exists(deliveryEpoch,:zero)+:one, "
                    "deliveryStartedAt=:now, updatedAt=:now"
                ),
                ConditionExpression=(
                    "#state=:ready AND requestSha256=:requestSha"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":inflight": "DELIVERY_IN_FLIGHT",
                    ":ready": "RESULT_READY",
                    ":owner": owner,
                    ":zero": 0,
                    ":one": 1,
                    ":requestSha": claim.request_sha256,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            current = dict(response["Attributes"])
        except Exception as error:
            current = self._read(claim.key)
            if current is None:
                raise LedgerFenceLost("delivery claim outcome is unknown") from error
        if (
            current.get("state") == "DELIVERY_IN_FLIGHT"
            and current.get("deliveryOwner") == owner
            and current.get("requestSha256") == claim.request_sha256
        ):
            claimed = self._claim(current)
            return LedgerClaim(
                key=claimed.key,
                request_sha256=claimed.request_sha256,
                state="DELIVERY_CLAIMED",
                result=claimed.result,
                owner=owner,
                epoch=int(current.get("deliveryEpoch", 0)),
            )
        return self._claim(current)

    def confirm_delivery(
        self, claim: LedgerClaim, receipt: Mapping[str, object]
    ) -> None:
        provider_id = receipt.get("providerMessageId") if isinstance(receipt, Mapping) else None
        if (
            claim.state != "DELIVERY_CLAIMED"
            or not claim.owner
            or claim.epoch is None
            or not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 256
        ):
            raise LedgerFenceLost("delivery receipt is invalid")
        now = int(self._clock_ms())
        try:
            self._table.update_item(
                Key={"eventId": claim.key},
                UpdateExpression=(
                    "SET #state=:delivered, providerMessageId=:providerId, "
                    "deliveredAt=:now, updatedAt=:now REMOVE deliveryOwner"
                ),
                ConditionExpression=(
                    "#state=:inflight AND deliveryOwner=:owner AND "
                    "deliveryEpoch=:epoch"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":delivered": "DELIVERED",
                    ":inflight": "DELIVERY_IN_FLIGHT",
                    ":providerId": provider_id,
                    ":owner": claim.owner,
                    ":epoch": claim.epoch,
                    ":now": now,
                },
                ReturnValues="ALL_NEW",
            )
            return
        except Exception as error:
            current = self._read(claim.key)
            if (
                current
                and current.get("state") == "DELIVERED"
                and current.get("providerMessageId") == provider_id
            ):
                return
            raise LedgerFenceLost("delivery confirmation outcome is uncertain") from error

    def mark_delivery_uncertain(
        self, claim: LedgerClaim, *, error_type: str
    ) -> None:
        if claim.state != "DELIVERY_CLAIMED" or not claim.owner or claim.epoch is None:
            return
        now = int(self._clock_ms())
        try:
            self._table.update_item(
                Key={"eventId": claim.key},
                UpdateExpression=(
                    "SET #state=:uncertain, uncertaintyReason=:reason, "
                    "updatedAt=:now REMOVE deliveryOwner"
                ),
                ConditionExpression=(
                    "#state=:inflight AND deliveryOwner=:owner AND "
                    "deliveryEpoch=:epoch"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":inflight": "DELIVERY_IN_FLIGHT",
                    ":uncertain": "DELIVERY_UNCERTAIN",
                    ":owner": claim.owner,
                    ":epoch": claim.epoch,
                    ":reason": str(error_type)[:128],
                    ":now": now,
                },
            )
        except Exception:
            return
