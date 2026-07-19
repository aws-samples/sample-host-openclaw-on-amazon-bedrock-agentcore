"""Durable one-time activation and strong-read portable live generation.

One user-bound DynamoDB item is the activation commit point.  A prepared
approval binds the authenticated user, complete bundle hash, immutable blob,
and currently observed generation. Activation conditionally advances that
generation, installs the blob pointer, consumes the approval, and adds the
bundle hash to a durable replay ledger in the same atomic update.

Imported receipts remain inert data inside this portable live generation;
they are never written into the action/effect namespace.  Likewise, imported
schedules remain disabled definitions and no connector authority envelope is
created.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import re
from typing import Mapping

from .manifest import (
    ImportRejected,
    ImportUncertain,
    canonical_json,
    strict_json_loads,
    user_id as _user_id,
)


_RECORD_TYPE = "PORTABLE_LIVE_STATE_V2"
_MAX_STAGED_BYTES = 70 * 1024 * 1024
_MAX_ACTIVATED_BUNDLES = 128
_SHA256 = re.compile(r"[0-9a-f]{64}")
_APPROVAL = re.compile(r"pia_[0-9a-f]{64}")


def _live_key(user_id: str) -> dict[str, str]:
    return {"PK": f"USER#{user_id}", "SK": "PORTABLE#LIVE_STATE"}


def _generation(value: object) -> int:
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ImportUncertain("portable generation is invalid")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ImportUncertain("portable generation is invalid")
    return value


def _bundle_hash(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ImportRejected("portable bundle hash is invalid")
    return value


def _activation_approval(user_id: str, bundle_hash: str, generation: int) -> str:
    digest = hashlib.sha256(b"personal-operator.portable-activation.v1\0")
    for value in (user_id, bundle_hash, str(generation)):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return f"pia_{digest.hexdigest()}"


def _approval(value: object) -> str:
    if not isinstance(value, str) or _APPROVAL.fullmatch(value) is None:
        raise ImportRejected("portable activation approval is invalid")
    return value


def _conditional_failure(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return bool(
        isinstance(response, Mapping)
        and response.get("Error", {}).get("Code")
        == "ConditionalCheckFailedException"
    )


def _staged_payload(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise ImportRejected("portable staged state is invalid")
    try:
        serialized = canonical_json(value)
    except (TypeError, ValueError) as error:
        raise ImportRejected("portable staged state is invalid") from error
    if len(serialized) > _MAX_STAGED_BYTES:
        raise ImportRejected("portable staged state exceeds its activation limit")
    # The JSON round-trip produces a detached, storage-compatible shape.
    parsed = strict_json_loads(serialized)
    if not isinstance(parsed, dict):
        raise ImportRejected("portable staged state is invalid")
    return parsed


class S3PortableBlobStore:
    """Immutable, user-prefixed backing for a complete staged generation."""

    def __init__(self, client, *, bucket_name: str) -> None:
        if (
            client is None
            or not isinstance(bucket_name, str)
            or not bucket_name
            or len(bucket_name) > 255
        ):
            raise ValueError("portable blob store is invalid")
        self._client = client
        self._bucket = bucket_name

    @staticmethod
    def _key(user_id: str, bundle_hash: str, generation: int) -> str:
        return (
            f"{user_id}/.system/portable/v2/imports/"
            f"{bundle_hash}/generation-{generation:020d}.json"
        )

    @staticmethod
    def _validated_key(user_id: str, value: object) -> str:
        prefix = f"{user_id}/.system/portable/v2/imports/"
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or len(value) > 768
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ImportUncertain("portable blob key is invalid")
        return value

    def load(self, user_id: str, *, blob_key: str, blob_sha256: str) -> dict:
        user_id = _user_id(user_id)
        key = self._validated_key(user_id, blob_key)
        expected = _bundle_hash(blob_sha256)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response.get("Body") if isinstance(response, Mapping) else None
            payload = body.read(_MAX_STAGED_BYTES + 1) if hasattr(body, "read") else None
        except Exception as error:
            raise ImportUncertain("portable blob read failed") from error
        if (
            not isinstance(payload, bytes)
            or len(payload) > _MAX_STAGED_BYTES
            or hashlib.sha256(payload).hexdigest() != expected
        ):
            raise ImportUncertain("portable blob content is invalid")
        try:
            parsed = strict_json_loads(payload)
        except (TypeError, ValueError) as error:
            raise ImportUncertain("portable blob content is invalid") from error
        try:
            staged = _staged_payload(parsed)
        except ImportRejected as error:
            raise ImportUncertain("portable blob content is invalid") from error
        if canonical_json(staged) != payload:
            raise ImportUncertain("portable blob is not canonical")
        return staged

    def stage(
        self,
        user_id: str,
        *,
        bundle_hash: str,
        expected_generation: int,
        staged,
    ) -> dict:
        user_id = _user_id(user_id)
        bundle_hash = _bundle_hash(bundle_hash)
        generation = _generation(expected_generation)
        staged = _staged_payload(staged)
        payload = canonical_json(staged)
        digest = hashlib.sha256(payload).hexdigest()
        key = self._key(user_id, bundle_hash, generation)
        try:
            response = self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
                IfNoneMatch="*",
            )
            if not isinstance(response, Mapping):
                raise RuntimeError("portable blob write returned invalid data")
        except Exception:
            # A deterministic retry finds the already-written immutable object.
            # Provider exception text is never exposed because it can contain
            # request metadata.
            try:
                existing = self.load(
                    user_id,
                    blob_key=key,
                    blob_sha256=digest,
                )
            except Exception as error:
                raise ImportUncertain("portable blob write failed") from error
            if existing != staged:
                raise ImportUncertain("portable blob write is ambiguous")
        else:
            # Strongly read the exact bytes before a Dynamo approval can point
            # at them; a successful Put response alone is not activation proof.
            if self.load(
                user_id,
                blob_key=key,
                blob_sha256=digest,
            ) != staged:
                raise ImportUncertain("portable blob write is ambiguous")
        return {"blobKey": key, "blobSha256": digest}


class DynamoStagedImportStore:
    """Atomic portable live-generation store backed by one DynamoDB item."""

    def __init__(self, table, *, blobs) -> None:
        if (
            table is None
            or not callable(getattr(blobs, "stage", None))
            or not callable(getattr(blobs, "load", None))
        ):
            raise ValueError("portable state table is required")
        self._table = table
        self._blobs = blobs

    def _read(self, user_id: str) -> Mapping | None:
        try:
            response = self._table.get_item(
                Key=_live_key(user_id), ConsistentRead=True
            )
        except Exception as error:
            raise ImportUncertain("portable state read failed") from error
        if not isinstance(response, Mapping):
            raise ImportUncertain("portable state read returned invalid data")
        item = response.get("Item")
        if item is None:
            return None
        if (
            not isinstance(item, Mapping)
            or item.get("PK") != f"USER#{user_id}"
            or item.get("SK") != "PORTABLE#LIVE_STATE"
            or item.get("recordType") != _RECORD_TYPE
            or item.get("userId") != user_id
        ):
            raise ImportUncertain("portable state record is invalid")
        _generation(item.get("generation"))
        history = item.get("activatedBundleHashes", set())
        if not isinstance(history, (set, frozenset)) or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in history
        ) or len(history) > _MAX_ACTIVATED_BUNDLES:
            raise ImportUncertain("portable replay ledger is invalid")
        pending = {
            "activationApproval",
            "approvalBundleHash",
            "approvalGeneration",
            "approvalBlobKey",
            "approvalBlobSha256",
        }
        present = pending.intersection(item)
        if present and (
            present != pending
            or _APPROVAL.fullmatch(str(item.get("activationApproval"))) is None
            or _SHA256.fullmatch(str(item.get("approvalBundleHash"))) is None
            or not isinstance(item.get("approvalBlobKey"), str)
            or _SHA256.fullmatch(str(item.get("approvalBlobSha256"))) is None
            or _generation(item.get("approvalGeneration"))
            != _generation(item.get("generation"))
        ):
            raise ImportUncertain("portable activation approval record is invalid")
        return item

    @staticmethod
    def _history(record: Mapping | None) -> frozenset[str]:
        if record is None:
            return frozenset()
        return frozenset(record.get("activatedBundleHashes", set()))

    def load_generation(self, user_id: str) -> int:
        user_id = _user_id(user_id)
        record = self._read(user_id)
        return 0 if record is None else _generation(record.get("generation"))

    def load_live(self, user_id: str) -> dict | None:
        """Strong-read the active imported surface, never a pending approval."""

        user_id = _user_id(user_id)
        record = self._read(user_id)
        if record is None or _generation(record.get("generation")) == 0:
            return None
        bundle_hash = _bundle_hash(record.get("liveBundleHash"))
        if bundle_hash not in self._history(record):
            raise ImportUncertain("portable live state is not replay-ledger bound")
        blob_key = record.get("liveBlobKey")
        blob_sha256 = record.get("liveBlobSha256")
        if not isinstance(blob_key, str) or not isinstance(blob_sha256, str):
            raise ImportUncertain("portable live blob reference is invalid")
        try:
            staged = _staged_payload(
                self._blobs.load(
                    user_id,
                    blob_key=blob_key,
                    blob_sha256=blob_sha256,
                )
            )
        except ImportRejected as error:
            raise ImportUncertain("portable live state is invalid") from error
        return {
            "userId": user_id,
            "generation": _generation(record.get("generation")),
            "bundleHash": bundle_hash,
            "staged": staged,
        }

    def prepare_activation(
        self,
        user_id: str,
        *,
        bundle_hash: str,
        expected_generation: int,
        staged,
    ) -> dict:
        user_id = _user_id(user_id)
        bundle_hash = _bundle_hash(bundle_hash)
        expected = _generation(expected_generation)
        staged = _staged_payload(staged)
        approval = _activation_approval(user_id, bundle_hash, expected)
        current = self._read(user_id)
        if bundle_hash in self._history(current):
            raise ImportRejected("portable bundle was already activated")
        if len(self._history(current)) >= _MAX_ACTIVATED_BUNDLES:
            raise ImportRejected("portable replay ledger is full")
        current_generation = 0 if current is None else _generation(
            current.get("generation")
        )
        if current_generation != expected:
            raise ImportRejected("portable generation changed before approval")
        if current is not None and current.get("activationApproval") not in {
            None,
            approval,
        }:
            raise ImportRejected("another portable activation is pending")
        blob = self._blobs.stage(
            user_id,
            bundle_hash=bundle_hash,
            expected_generation=expected,
            staged=staged,
        )
        if (
            not isinstance(blob, Mapping)
            or set(blob) != {"blobKey", "blobSha256"}
            or not isinstance(blob.get("blobKey"), str)
            or not isinstance(blob.get("blobSha256"), str)
            or _SHA256.fullmatch(blob["blobSha256"]) is None
        ):
            raise ImportUncertain("portable blob reference is invalid")

        values = {
            ":recordType": _RECORD_TYPE,
            ":userId": user_id,
            ":expected": expected,
            ":zero": 0,
            ":approval": approval,
            ":bundleHash": bundle_hash,
            ":blobKey": blob["blobKey"],
            ":blobSha256": blob["blobSha256"],
        }
        try:
            response = self._table.update_item(
                Key=_live_key(user_id),
                UpdateExpression=(
                    "SET #recordType=if_not_exists(#recordType,:recordType), "
                    "#userId=if_not_exists(#userId,:userId), "
                    "#generation=if_not_exists(#generation,:zero), "
                    "#activationApproval=:approval, "
                    "#approvalBundleHash=:bundleHash, "
                    "#approvalGeneration=:expected, "
                    "#approvalBlobKey=:blobKey, "
                    "#approvalBlobSha256=:blobSha256"
                ),
                ConditionExpression=(
                    "((attribute_not_exists(#pk) AND attribute_not_exists(#sk) "
                    "AND :expected=:zero) OR "
                    "(#recordType=:recordType AND #userId=:userId AND "
                    "#generation=:expected)) AND "
                    "(attribute_not_exists(#activatedBundleHashes) OR "
                    "NOT contains(#activatedBundleHashes,:bundleHash)) AND "
                    "(attribute_not_exists(#activationApproval) OR "
                    "#activationApproval=:approval)"
                ),
                ExpressionAttributeNames={
                    "#pk": "PK",
                    "#sk": "SK",
                    "#recordType": "recordType",
                    "#userId": "userId",
                    "#generation": "generation",
                    "#activationApproval": "activationApproval",
                    "#approvalBundleHash": "approvalBundleHash",
                    "#approvalGeneration": "approvalGeneration",
                    "#approvalBlobKey": "approvalBlobKey",
                    "#approvalBlobSha256": "approvalBlobSha256",
                    "#activatedBundleHashes": "activatedBundleHashes",
                },
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            attributes = response.get("Attributes") if isinstance(response, Mapping) else None
        except Exception as error:
            reconciled = self._read(user_id)
            if bundle_hash in self._history(reconciled):
                raise ImportRejected("portable bundle was already activated") from error
            if self._prepared_matches(
                reconciled,
                approval=approval,
                bundle_hash=bundle_hash,
                expected=expected,
                blob=blob,
            ):
                attributes = reconciled
            elif _conditional_failure(error):
                raise ImportRejected("portable activation approval was not prepared") from error
            else:
                raise ImportUncertain("portable activation approval write failed") from error
        if not self._prepared_matches(
            attributes,
            approval=approval,
            bundle_hash=bundle_hash,
            expected=expected,
            blob=blob,
        ):
            reconciled = self._read(user_id)
            if not self._prepared_matches(
                reconciled,
                approval=approval,
                bundle_hash=bundle_hash,
                expected=expected,
                blob=blob,
            ):
                raise ImportUncertain(
                    "portable activation approval write was ambiguous"
                )
        return {
            "activationApproval": approval,
            "bundleHash": bundle_hash,
            "expectedGeneration": expected,
        }

    @staticmethod
    def _prepared_matches(
        record: object,
        *,
        approval: str,
        bundle_hash: str,
        expected: int,
        blob: Mapping,
    ) -> bool:
        try:
            return bool(
                isinstance(record, Mapping)
                and record.get("activationApproval") == approval
                and record.get("approvalBundleHash") == bundle_hash
                and _generation(record.get("approvalGeneration")) == expected
                and record.get("approvalBlobKey") == blob["blobKey"]
                and record.get("approvalBlobSha256") == blob["blobSha256"]
            )
        except ImportUncertain:
            return False

    def activate_once(
        self,
        user_id: str,
        *,
        bundle_hash: str,
        activation_approval: str,
        expected_generation: int,
        staged,
    ) -> int:
        user_id = _user_id(user_id)
        bundle_hash = _bundle_hash(bundle_hash)
        approval = _approval(activation_approval)
        expected = _generation(expected_generation)
        staged = _staged_payload(staged)
        current = self._read(user_id)
        if bundle_hash in self._history(current):
            raise ImportRejected("portable bundle was already activated")
        if (
            current is None
            or _generation(current.get("generation")) != expected
            or current.get("activationApproval") != approval
            or current.get("approvalBundleHash") != bundle_hash
            or _generation(current.get("approvalGeneration")) != expected
        ):
            raise ImportRejected("portable activation approval is invalid or stale")
        if len(self._history(current)) >= _MAX_ACTIVATED_BUNDLES:
            raise ImportRejected("portable replay ledger is full")
        blob = self._blobs.stage(
            user_id,
            bundle_hash=bundle_hash,
            expected_generation=expected,
            staged=staged,
        )
        if (
            not isinstance(blob, Mapping)
            or set(blob) != {"blobKey", "blobSha256"}
            or current.get("approvalBlobKey") != blob.get("blobKey")
            or current.get("approvalBlobSha256") != blob.get("blobSha256")
        ):
            raise ImportRejected("portable activation blob is not approved")

        next_generation = expected + 1
        values = {
            ":recordType": _RECORD_TYPE,
            ":userId": user_id,
            ":expected": expected,
            ":nextGeneration": next_generation,
            ":approval": approval,
            ":bundleHash": bundle_hash,
            ":bundleHashSet": {bundle_hash},
            ":blobKey": blob["blobKey"],
            ":blobSha256": blob["blobSha256"],
        }
        try:
            response = self._table.update_item(
                Key=_live_key(user_id),
                UpdateExpression=(
                    "SET #liveBundleHash=:bundleHash, #liveBlobKey=:blobKey, "
                    "#liveBlobSha256=:blobSha256, #generation=:nextGeneration "
                    "REMOVE #activationApproval, #approvalBundleHash, "
                    "#approvalGeneration, #approvalBlobKey, #approvalBlobSha256 "
                    "ADD #activatedBundleHashes :bundleHashSet"
                ),
                ConditionExpression=(
                    "#recordType=:recordType AND #userId=:userId AND "
                    "#generation=:expected AND #activationApproval=:approval AND "
                    "#approvalBundleHash=:bundleHash AND "
                    "#approvalGeneration=:expected AND "
                    "#approvalBlobKey=:blobKey AND "
                    "#approvalBlobSha256=:blobSha256 AND "
                    "(attribute_not_exists(#activatedBundleHashes) OR "
                    "NOT contains(#activatedBundleHashes,:bundleHash))"
                ),
                ExpressionAttributeNames={
                    "#recordType": "recordType",
                    "#userId": "userId",
                    "#generation": "generation",
                    "#activationApproval": "activationApproval",
                    "#approvalBundleHash": "approvalBundleHash",
                    "#approvalGeneration": "approvalGeneration",
                    "#approvalBlobKey": "approvalBlobKey",
                    "#approvalBlobSha256": "approvalBlobSha256",
                    "#activatedBundleHashes": "activatedBundleHashes",
                    "#liveBundleHash": "liveBundleHash",
                    "#liveBlobKey": "liveBlobKey",
                    "#liveBlobSha256": "liveBlobSha256",
                },
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            attributes = response.get("Attributes") if isinstance(response, Mapping) else None
        except Exception as error:
            reconciled = self._read(user_id)
            if _conditional_failure(error):
                if bundle_hash in self._history(reconciled):
                    raise ImportRejected("portable bundle was already activated") from error
                raise ImportRejected("portable activation approval is invalid or stale") from error
            if self._activated_matches(
                reconciled,
                next_generation=next_generation,
                bundle_hash=bundle_hash,
                blob=blob,
            ):
                return next_generation
            raise ImportUncertain("portable activation write failed") from error
        if not self._activated_matches(
            attributes,
            next_generation=next_generation,
            bundle_hash=bundle_hash,
            blob=blob,
        ):
            reconciled = self._read(user_id)
            if not self._activated_matches(
                reconciled,
                next_generation=next_generation,
                bundle_hash=bundle_hash,
                blob=blob,
            ):
                raise ImportUncertain("portable activation write was ambiguous")
        return next_generation

    def _activated_matches(
        self,
        record: object,
        *,
        next_generation: int,
        bundle_hash: str,
        blob: Mapping,
    ) -> bool:
        try:
            return bool(
                isinstance(record, Mapping)
                and _generation(record.get("generation")) == next_generation
                and record.get("liveBundleHash") == bundle_hash
                and record.get("liveBlobKey") == blob["blobKey"]
                and record.get("liveBlobSha256") == blob["blobSha256"]
                and bundle_hash in self._history(record)
                and "activationApproval" not in record
            )
        except ImportUncertain:
            return False
